from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.base import utcnow
from bmsbot.db.models import Monitor
from bmsbot.domain.enums import MonitorKind


class MonitorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure(
        self,
        region_code: str,
        event_code: str,
        date_code: str,
        kind: MonitorKind = MonitorKind.SHOWTIMES,
        venue_code: str = "",
        interval_sec: int = 10,
        priority: int = 100,
    ) -> Monitor:
        result = await self.session.execute(
            select(Monitor).where(
                Monitor.region_code == region_code,
                Monitor.event_code == event_code,
                Monitor.date_code == date_code,
                Monitor.kind == kind.value,
                Monitor.venue_code == venue_code,
            )
        )
        monitor = result.scalar_one_or_none()
        if monitor is None:
            monitor = Monitor(
                region_code=region_code,
                event_code=event_code,
                date_code=date_code,
                kind=kind.value,
                venue_code=venue_code,
                interval_sec=interval_sec,
                priority=priority,
                next_run_at=utcnow(),
            )
            self.session.add(monitor)
            await self.session.flush()
        else:
            monitor.is_active = True
            monitor.interval_sec = interval_sec
            monitor.priority = min(monitor.priority, priority)
            await self.session.flush()
        return monitor

    async def get(self, monitor_id: int) -> Monitor | None:
        return await self.session.get(Monitor, monitor_id)

    async def due(self, limit: int = 20, worker_id: str = "worker", lease_sec: int = 120) -> list[Monitor]:
        now = utcnow()
        result = await self.session.execute(
            select(Monitor)
            .where(
                Monitor.is_active.is_(True),
                Monitor.next_run_at <= now,
                or_(Monitor.lease_until.is_(None), Monitor.lease_until < now),
            )
            .order_by(Monitor.priority.asc(), Monitor.next_run_at.asc())
            .limit(limit)
        )
        monitors = list(result.scalars())
        for monitor in monitors:
            await self.session.execute(
                update(Monitor)
                .where(Monitor.id == monitor.id)
                .values(lease_owner=worker_id, lease_until=now + timedelta(seconds=lease_sec))
            )
        await self.session.flush()
        return monitors

    async def release(self, monitor: Monitor, next_run_at: datetime | None = None) -> None:
        monitor.lease_owner = None
        monitor.lease_until = None
        monitor.next_run_at = next_run_at or utcnow() + timedelta(seconds=monitor.interval_sec)
        await self.session.flush()

    async def mark_success(
        self, monitor: Monitor, *, changed: bool, latency_ms: int, has_shows: bool
    ) -> None:
        now = utcnow()
        monitor.last_run_at = now
        monitor.lag_ms = max(0, int((now - monitor.next_run_at).total_seconds() * 1000))
        monitor.last_error = None
        monitor.consecutive_failures = 0
        monitor.has_shows = has_shows
        if has_shows and monitor.first_open_at is None:
            monitor.first_open_at = now
        if changed:
            monitor.last_change_at = now
        await self.release(monitor, now + timedelta(seconds=monitor.interval_sec))

    async def mark_failure(self, monitor: Monitor, error: str) -> None:
        monitor.last_run_at = utcnow()
        monitor.last_error = error[:2000]
        monitor.consecutive_failures += 1
        backoff = min(300, monitor.interval_sec * (2 ** min(monitor.consecutive_failures, 4)))
        monitor.next_run_at = utcnow() + timedelta(seconds=backoff)
        monitor.lease_owner = None
        monitor.lease_until = None
        await self.session.flush()

    async def set_active(self, monitor: Monitor, active: bool) -> None:
        monitor.is_active = active
        if active:
            monitor.next_run_at = utcnow()
        await self.session.flush()

    async def list_page(self, limit: int = 50, offset: int = 0) -> list[Monitor]:
        result = await self.session.execute(
            select(Monitor).order_by(Monitor.next_run_at).limit(limit).offset(offset)
        )
        return list(result.scalars())

    async def count_due(self) -> int:
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count(Monitor.id)).where(
                Monitor.is_active.is_(True),
                Monitor.next_run_at <= utcnow(),
                and_(or_(Monitor.lease_until.is_(None), Monitor.lease_until < utcnow())),
            )
        )
        return int(result.scalar_one())

    async def count_active(self) -> int:
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count(Monitor.id)).where(Monitor.is_active.is_(True))
        )
        return int(result.scalar_one())

    async def deactivate_orphans(self) -> int:
        """Pause monitors that no active watch references anymore."""
        from sqlalchemy import exists, not_, update

        from bmsbot.db.models import Watch

        watched = exists().where(
            Watch.region_code == Monitor.region_code,
            Watch.event_code == Monitor.event_code,
            Watch.is_active.is_(True),
        )
        result = await self.session.execute(
            update(Monitor).where(not_(watched)).values(is_active=False)
        )
        await self.session.flush()
        return int(result.rowcount or 0)
