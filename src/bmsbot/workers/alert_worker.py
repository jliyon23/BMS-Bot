from __future__ import annotations

import asyncio
import logging
import signal
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bmsbot.config import Settings, get_settings
from bmsbot.db.base import utcnow
from bmsbot.db.models import AlertEvent, User, WorkerHeartbeat
from bmsbot.db.repositories.alerts import AlertRepository
from bmsbot.db.session import session_factory
from bmsbot.domain.enums import AlertStatus, AlertType
from bmsbot.logging import configure_logging
from bmsbot.services.formatting import format_batch
from bmsbot.services.telegram import TelegramSender

log = logging.getLogger(__name__)


class AlertWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
        sender: TelegramSender,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.sender = sender
        self._stop = asyncio.Event()
        self._failed = 0
        self._retried = 0
        self._last_error: str | None = None

    async def run_forever(self) -> None:
        log.info("alert worker started", extra={"worker": "alert-sender"})
        if not self.sender.configured:
            # Refuse to run blind: without a token every alert would be dropped.
            log.error(
                "BMS_TELEGRAM_BOT_TOKEN is not configured; refusing to start alert worker",
                extra={"worker": "alert-sender"},
            )
            raise SystemExit("BMS_TELEGRAM_BOT_TOKEN is not configured")
        while not self._stop.is_set():
            try:
                self._failed = 0
                self._retried = 0
                sent = await self.process_once()
                await self._heartbeat(sent)
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("alert loop failed", extra={"worker": "alert-sender"})
                await asyncio.sleep(3)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def process_once(self, limit: int = 200) -> int:
        async with self.session_factory() as session:
            alerts = await AlertRepository(session).pending(limit)
            if not alerts:
                return 0
            user_ids = {alert.user_id for alert in alerts}
            result = await session.execute(select(User).where(User.id.in_(user_ids)))
            users = {user.id: user for user in result.scalars()}

        batches: dict[tuple[int, str, str], list[AlertEvent]] = defaultdict(list)
        for alert in alerts:
            payload = alert.payload or {}
            key = (
                alert.user_id,
                str(payload.get("date_code") or ""),
                str(payload.get("movie_title") or ""),
            )
            batches[key].append(alert)

        sent_count = 0
        for (user_id, date_code, movie_title), batch in batches.items():
            user = users.get(user_id)
            if user is None or not user.is_active or user.is_banned:
                await self._handle_failure(batch, "user unavailable", retryable=False)
                continue
            region_name = str((batch[0].payload or {}).get("region_name") or "")
            changes: list[tuple[AlertType, dict]] = []
            for alert in batch:
                try:
                    changes.append((AlertType(alert.alert_type), alert.payload or {}))
                except ValueError:
                    continue
            message = format_batch(
                movie_title=movie_title or "Movie",
                region_name=region_name or "BMS",
                date_code=date_code or utcnow().strftime("%Y%m%d"),
                changes=changes,
            )
            ok = await self.sender.send_html(user.telegram_id, message)
            if ok:
                await self._mark_sent([alert.id for alert in batch])
                sent_count += len(batch)
                self._last_error = None
            else:
                await self._handle_failure(batch, "telegram send failed", retryable=True)

        return sent_count

    async def _mark_sent(self, alert_ids: list[int]) -> None:
        from sqlalchemy import update

        async with self.session_factory() as session:
            await session.execute(
                update(AlertEvent)
                .where(AlertEvent.id.in_(alert_ids))
                .values(status=AlertStatus.SENT.value, sent_at=utcnow(), last_error=None)
            )
            await session.commit()

    async def _mark_failed(self, alert_ids: list[int], error: str) -> None:
        from sqlalchemy import update

        async with self.session_factory() as session:
            await session.execute(
                update(AlertEvent)
                .where(AlertEvent.id.in_(alert_ids))
                .values(
                    status=AlertStatus.FAILED.value,
                    attempts=AlertEvent.attempts + 1,
                    last_error=error[:500],
                )
            )
            await session.commit()

    async def _handle_failure(
        self, alerts: list[AlertEvent], error: str, *, retryable: bool
    ) -> None:
        """Retry transient failures for a short window, then give up.

        BMS alerts are time-sensitive, so a message older than
        ``alert_retry_ttl_sec`` is marked FAILED instead of being replayed late.
        """
        from sqlalchemy import update

        now = utcnow()
        ttl = timedelta(seconds=self.settings.alert_retry_ttl_sec)
        max_attempts = self.settings.alert_max_attempts
        async with self.session_factory() as session:
            for alert in alerts:
                attempts = alert.attempts + 1
                expired = alert.created_at is not None and (now - alert.created_at) > ttl
                final = not retryable or attempts >= max_attempts or expired
                values: dict[str, object] = {
                    "attempts": attempts,
                    "last_error": error[:500],
                }
                if final:
                    values["status"] = AlertStatus.FAILED.value
                    self._failed += 1
                else:
                    backoff = min(30, 5 * attempts)
                    values["next_attempt_at"] = now + timedelta(seconds=backoff)
                    self._retried += 1
                await session.execute(
                    update(AlertEvent).where(AlertEvent.id == alert.id).values(**values)
                )
            await session.commit()
        self._last_error = error

    async def _heartbeat(self, sent: int) -> None:
        async with self.session_factory() as session:
            pending = await AlertRepository(session).count_pending()
            heartbeat = await session.get(WorkerHeartbeat, "alert-sender")
            if heartbeat is None:
                heartbeat = WorkerHeartbeat(name="alert-sender", status="ok", detail={})
                session.add(heartbeat)
            heartbeat.status = "degraded" if self._failed else "ok"
            heartbeat.detail = {
                "sent": sent,
                "failed": self._failed,
                "retried": self._retried,
                "pending": pending,
                "configured": self.sender.configured,
                "last_error": self._last_error,
                "at": utcnow().isoformat(),
            }
            heartbeat.last_seen_at = utcnow()
            await session.commit()

    def stop(self) -> None:
        self._stop.set()


async def main() -> None:
    configure_logging()
    settings = get_settings()
    sender = TelegramSender(settings.telegram_bot_token)
    worker = AlertWorker(session_factory, settings, sender)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run_forever()
    finally:
        await sender.close()


if __name__ == "__main__":
    asyncio.run(main())
