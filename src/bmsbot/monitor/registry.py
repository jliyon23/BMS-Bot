from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.bms.client import BmsClient
from bmsbot.bms.parsers import ShowtimesSnapshot
from bmsbot.config import Settings
from bmsbot.db.models import Monitor, Region, Watch
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.domain.enums import AlertType, MonitorKind


def watch_priority(watch: Watch) -> int:
    """Seat-level subscribers get scheduled ahead of showtime-only watchers."""
    for pref in watch.prefs:
        if pref.enabled and pref.alert_type in (
            AlertType.SEAT_COUNT.value,
            AlertType.SOLD_OUT.value,
            AlertType.STATUS_CHANGE.value,
        ):
            return 10
    return 100


async def sync_watch_monitors(
    session: AsyncSession,
    client: BmsClient,
    watch: Watch,
    region: Region,
    settings: Settings,
    snapshot: ShowtimesSnapshot | None = None,
) -> list[Monitor]:
    """Attach a watch to the shared monitors for its region/movie/dates."""
    if snapshot is None:
        from bmsbot.bms.client import RegionRef

        snapshot = await client.showtimes(
            watch.event_code,
            datetime.now().strftime("%Y%m%d"),
            RegionRef(region.code, region.slug, region.name, region.lat, region.lng),
        )

    dates = list(snapshot.available_dates)
    if snapshot.actual_date and snapshot.actual_date not in dates:
        dates.append(snapshot.actual_date)
    if not dates:
        dates = [datetime.now().strftime("%Y%m%d")]

    repo = MonitorRepository(session)
    monitors: list[Monitor] = []
    for date_code in dates:
        monitor = await repo.ensure(
            region.code,
            watch.event_code,
            date_code,
            kind=MonitorKind.SHOWTIMES,
            interval_sec=settings.poll_interval_sec,
            priority=watch_priority(watch),
        )
        monitors.append(monitor)
    await session.flush()
    return monitors


async def attach_watch_to_existing_monitors(
    session: AsyncSession, watch: Watch
) -> int:
    """When a second user subscribes, reuse the exact same monitors."""
    result = await session.execute(
        select(Monitor).where(
            Monitor.region_code == watch.region_code,
            Monitor.event_code == watch.event_code,
            Monitor.kind == MonitorKind.SHOWTIMES.value,
        )
    )
    monitors = list(result.scalars())
    priority = watch_priority(watch)
    for monitor in monitors:
        monitor.is_active = True
        monitor.priority = min(monitor.priority, priority)
    await session.flush()
    return len(monitors)
