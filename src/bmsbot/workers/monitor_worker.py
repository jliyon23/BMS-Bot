from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy.ext.asyncio import async_sessionmaker

from bmsbot.bms.client import BmsClient
from bmsbot.bms.http import AsyncRateLimiter
from bmsbot.config import Settings, get_settings
from bmsbot.db.base import utcnow
from bmsbot.db.models import Monitor, WorkerHeartbeat
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.db.session import session_factory
from bmsbot.monitor.scanner import MonitorScanner
from bmsbot.proxies.pool import ProxyPool
from bmsbot.services.runtime import RuntimeFlags

log = logging.getLogger(__name__)


class MonitorWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
        client: BmsClient,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.client = client
        self.scanner = MonitorScanner(session_factory, settings, client)
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        log.info("monitor worker started", extra={"worker": self.settings.worker_id})
        semaphore = asyncio.Semaphore(self.settings.max_concurrent_scrapes)
        while not self._stop.is_set():
            try:
                async with self.session_factory() as session:
                    monitors = await MonitorRepository(session).due(
                        limit=self.settings.max_concurrent_scrapes,
                        worker_id=self.settings.worker_id,
                        lease_sec=180,
                    )
                    await session.commit()

                async def run(monitor_id: int) -> None:
                    async with semaphore:
                        try:
                            await self.scanner.scan(monitor_id)
                        except Exception as exc:
                            log.exception(
                                "monitor %s failed", monitor_id, extra={"monitor_id": monitor_id}
                            )
                            async with self.session_factory() as session:
                                monitor = await session.get(Monitor, monitor_id)
                                if monitor is not None:
                                    await MonitorRepository(session).mark_failure(
                                        monitor, f"{type(exc).__name__}: {exc}"
                                    )
                                    await session.commit()

                if monitors:
                    await asyncio.gather(*(run(monitor.id) for monitor in monitors))
                await self._heartbeat(len(monitors))
            except Exception:
                log.exception("monitor loop failed", extra={"worker": self.settings.worker_id})
                await asyncio.sleep(5)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def _heartbeat(self, due_count: int) -> None:
        async with self.session_factory() as session:
            heartbeat = await session.get(WorkerHeartbeat, self.settings.worker_id)
            if heartbeat is None:
                heartbeat = WorkerHeartbeat(name=self.settings.worker_id, status="ok", detail={})
                session.add(heartbeat)
            heartbeat.status = "ok"
            heartbeat.detail = {"last_cycle_monitors": due_count, "at": utcnow().isoformat()}
            heartbeat.last_seen_at = utcnow()
            await session.commit()

    def stop(self) -> None:
        self._stop.set()


async def main() -> None:
    from bmsbot.logging import configure_logging

    configure_logging()
    settings = get_settings()
    pool = ProxyPool(session_factory, settings)
    flags = RuntimeFlags(session_factory)
    client = BmsClient(
        settings,
        pool,
        limiter=AsyncRateLimiter(settings.max_requests_per_sec),
        proxy_enabled=flags.use_proxy,
    )
    worker = MonitorWorker(session_factory, settings, client)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)

    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
