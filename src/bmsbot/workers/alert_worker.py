from __future__ import annotations

import asyncio
import logging
import signal
from collections import defaultdict

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

    async def run_forever(self) -> None:
        log.info("alert worker started", extra={"worker": "alert-sender"})
        while not self._stop.is_set():
            try:
                sent = await self.process_once()
                await self._heartbeat(sent)
            except Exception:
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
                await self._mark_failed([alert.id for alert in batch], "user unavailable")
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
            else:
                await self._mark_failed([alert.id for alert in batch], "telegram send failed")

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

    async def _heartbeat(self, sent: int) -> None:
        async with self.session_factory() as session:
            heartbeat = await session.get(WorkerHeartbeat, "alert-sender")
            if heartbeat is None:
                heartbeat = WorkerHeartbeat(name="alert-sender", status="ok", detail={})
                session.add(heartbeat)
            heartbeat.status = "ok"
            heartbeat.detail = {"sent": sent, "at": utcnow().isoformat()}
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
