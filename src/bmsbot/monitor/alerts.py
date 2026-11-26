from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.models import Monitor
from bmsbot.db.repositories.alerts import AlertRepository
from bmsbot.db.repositories.watches import WatchRepository
from bmsbot.domain.enums import AlertType
from bmsbot.monitor.diff import Change


async def enqueue_changes(
    session: AsyncSession,
    *,
    monitor: Monitor,
    date_code: str,
    movie_title: str,
    region_name: str,
    changes: list[Change],
) -> int:
    """Fan a single monitor diff out to subscribed users exactly once."""
    if not changes:
        return 0

    watches = await WatchRepository(session).list_active_for_event(
        monitor.region_code, monitor.event_code
    )
    alerts = AlertRepository(session)
    created = 0

    for watch in watches:
        enabled = {pref.alert_type: pref.enabled for pref in watch.prefs}
        for change in changes:
            if not enabled.get(change.alert_type.value, False):
                continue
            payload = {
                **change.payload,
                "date_code": date_code,
                "movie_title": movie_title,
                "region_name": region_name,
            }
            dedupe_key = (
                f"{watch.id}:{monitor.id}:{change.alert_type.value}:{change.dedupe_fragment}"
            )
            inserted = await alerts.enqueue(
                user_id=watch.user_id,
                watch_id=watch.id,
                monitor_id=monitor.id,
                alert_type=change.alert_type.value,
                dedupe_key=dedupe_key,
                payload=payload,
            )
            created += int(inserted)

    return created


async def seat_alerts_enabled(
    session: AsyncSession, region_code: str, event_code: str
) -> bool:
    """True when any subscriber wants sold-out/status/seat-count alerts."""
    wanted = {
        AlertType.SOLD_OUT.value,
        AlertType.STATUS_CHANGE.value,
        AlertType.SEAT_COUNT.value,
    }
    watches = await WatchRepository(session).list_active_for_event(region_code, event_code)
    for watch in watches:
        for pref in watch.prefs:
            if pref.enabled and pref.alert_type in wanted:
                return True
    return False
