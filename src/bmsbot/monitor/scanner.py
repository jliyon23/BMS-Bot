from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bmsbot.bms.client import BmsClient, BmsError, NoProxyAvailable
from bmsbot.bms.parsers import ParsedShow, ShowtimesSnapshot, effective_availability
from bmsbot.config import Settings
from bmsbot.db.base import utcnow
from bmsbot.db.models import Monitor, Movie, Region, Show, Theatre
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.db.repositories.movies import MovieRepository
from bmsbot.db.repositories.settings import SettingsRepository
from bmsbot.domain.enums import AlertType, MonitorKind
from bmsbot.monitor.alerts import enqueue_changes, seat_alerts_enabled
from bmsbot.monitor.diff import Change, ShowState, booking_opened, diff_showtimes

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


class MonitorScanner:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
        client: BmsClient,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.client = client

    async def scan(self, monitor_id: int) -> None:
        async with self.session_factory() as session:
            monitor = await session.get(Monitor, monitor_id)
            if monitor is None or not monitor.is_active:
                return
            movie = await session.get(Movie, monitor.event_code)
            region = await session.get(Region, monitor.region_code)
            if movie is None or region is None:
                monitor.is_active = False
                await session.commit()
                return

            try:
                snapshot = await self.client.showtimes(
                    monitor.event_code, monitor.date_code, _region_ref(region)
                )
            except NoProxyAvailable:
                # Do not count an empty proxy pool as a monitor failure.
                monitor.last_error = "no proxy available"
                monitor.next_run_at = utcnow() + timedelta(seconds=monitor.interval_sec)
                monitor.lease_owner = None
                monitor.lease_until = None
                await session.commit()
                return
            except BmsError as exc:
                await MonitorRepository(session).mark_failure(monitor, str(exc))
                await session.commit()
                return

            today = datetime.now(IST).strftime("%Y%m%d")
            if monitor.date_code < today:
                # The date is over. Retire it quietly instead of emitting a
                # SHOW_REMOVED storm for every session.
                await self._deactivate_shows(session, monitor)
                monitor.is_active = False
                monitor.next_run_at = utcnow() + timedelta(days=1)
                monitor.lease_owner = None
                monitor.lease_until = None
                await session.commit()
                return

            # BMS returns the next open date when the requested one is empty.
            # Never attribute those shows to the requested date, and do not
            # turn a fallback into a mass "show removed" event.
            if snapshot.actual_date and snapshot.actual_date != monitor.date_code:
                await self._deactivate_shows(session, monitor)
                # Treat "not open yet" as a valid baseline, so the eventual
                # opening of this date produces a BOOKING_OPEN alert.
                monitor.primed = True
                await MonitorRepository(session).mark_success(
                    monitor, changed=False, latency_ms=0, has_shows=False
                )
                await session.commit()
                return

            first_scan = not monitor.primed
            new_date_alerts = await self._ensure_date_monitors(
                session, monitor, snapshot.available_dates, today
            )

            previous = await self._load_previous(session, monitor)
            current_states = [self._state(show) for theatre in snapshot.theatres for show in theatre.shows]

            runtime = SettingsRepository(session)
            seat_threshold = int(
                await runtime.get("seat_alert_threshold", self.settings.seat_alert_threshold)
            )
            max_seat_checks = int(
                await runtime.get(
                    "max_seat_checks_per_scan", self.settings.max_seat_checks_per_scan
                )
            )

            if await seat_alerts_enabled(session, monitor.region_code, monitor.event_code):
                current_states = await self._enrich_seat_status(
                    session, monitor, region, current_states, max_seat_checks
                )

            # The first successful scan is a baseline. Otherwise a new
            # subscriber would receive a "new theatre" storm for every cinema
            # already showing the movie.
            if first_scan:
                changes = []
            else:
                changes = diff_showtimes(
                    previous,
                    current_states,
                    seat_threshold=seat_threshold,
                )
                opened = booking_opened(
                    monitor.has_shows, bool(current_states), monitor.date_code
                )
                if opened is not None:
                    changes.append(opened)

            await self._persist_shows(session, monitor, movie, snapshot, current_states)
            monitor.primed = True
            changes = await self._apply_cooldowns(session, monitor, changes)

            created = await enqueue_changes(
                session,
                monitor=monitor,
                date_code=monitor.date_code,
                movie_title=movie.title,
                region_name=region.name,
                changes=changes,
            )
            if not first_scan:
                for new_date, change in new_date_alerts:
                    created += await enqueue_changes(
                        session,
                        monitor=monitor,
                        date_code=new_date,
                        movie_title=movie.title,
                        region_name=region.name,
                        changes=[change],
                    )

            await MonitorRepository(session).mark_success(
                monitor,
                changed=bool(changes),
                latency_ms=0,
                has_shows=bool(current_states),
            )
            await session.commit()
            if created:
                log.info(
                    "monitor %s produced %d alerts",
                    monitor.id,
                    created,
                    extra={"monitor_id": monitor.id, "event_code": monitor.event_code},
                )

    async def _ensure_date_monitors(
        self,
        session: AsyncSession,
        monitor: Monitor,
        available_dates: list[str],
        today: str,
    ) -> list[tuple[str, Any]]:
        """Create monitors for newly opened dates and return booking-open alerts."""
        result = await session.execute(
            select(Monitor).where(
                Monitor.region_code == monitor.region_code,
                Monitor.event_code == monitor.event_code,
                Monitor.kind == monitor.kind,
            )
        )
        existing = {row.date_code: row for row in result.scalars()}
        alerts: list[tuple[str, Any]] = []
        repo = MonitorRepository(session)
        for date_code in available_dates:
            if date_code == monitor.date_code or date_code < today:
                continue
            current = existing.get(date_code)
            if current is not None:
                if not current.is_active:
                    current.is_active = True
                    current.next_run_at = utcnow()
                continue
            await repo.ensure(
                monitor.region_code,
                monitor.event_code,
                date_code,
                kind=MonitorKind(monitor.kind),
                interval_sec=monitor.interval_sec,
                priority=monitor.priority,
            )
            alerts.append(
                (
                    date_code,
                    Change(
                        alert_type=AlertType.BOOKING_OPEN,
                        dedupe_fragment=f"open:{date_code}",
                        payload={"date_code": date_code},
                    ),
                )
            )
        await session.flush()
        return alerts

    async def _apply_cooldowns(
        self, session: AsyncSession, monitor: Monitor, changes: list
    ) -> list:
        """Rate-limit noisy per-show alerts without losing distinct transitions."""
        if not changes:
            return changes
        result = await session.execute(
            select(Show).where(
                Show.event_code == monitor.event_code,
                Show.date_code == monitor.date_code,
            )
        )
        shows = {(show.venue_code, show.session_id): show for show in result.scalars()}
        now = utcnow()
        kept = []
        for change in changes:
            if change.alert_type not in (AlertType.SEAT_COUNT, AlertType.SOLD_OUT):
                kept.append(change)
                continue
            key = (change.payload.get("venue_code"), change.payload.get("session_id"))
            show = shows.get(key)
            if show is None:
                kept.append(change)
                continue
            if change.alert_type is AlertType.SEAT_COUNT:
                last = show.last_seat_alert_at
                if last and (now - last).total_seconds() < self.settings.seat_alert_cooldown_sec:
                    continue
                show.last_seat_alert_at = now
            else:
                last = show.last_sold_out_alert_at
                if last and (now - last).total_seconds() < 1800:
                    continue
                show.last_sold_out_alert_at = now
            kept.append(change)
        await session.flush()
        return kept

    async def _deactivate_shows(self, session: AsyncSession, monitor: Monitor) -> None:
        result = await session.execute(
            select(Show).where(
                Show.event_code == monitor.event_code,
                Show.date_code == monitor.date_code,
                Show.is_active.is_(True),
            )
        )
        for show in result.scalars():
            show.is_active = False
        await session.flush()

    async def _load_previous(self, session: AsyncSession, monitor: Monitor) -> list[ShowState]:
        result = await session.execute(
            select(Show, Theatre.name)
            .join(Theatre, Theatre.venue_code == Show.venue_code)
            .where(
                Show.event_code == monitor.event_code,
                Show.date_code == monitor.date_code,
                Show.is_active.is_(True),
            )
        )
        return [self._state(show, venue_name=name) for show, name in result.all()]

    @staticmethod
    def _state(show: ParsedShow | Show, venue_name: str | None = None) -> ShowState:
        return ShowState(
            venue_code=show.venue_code,
            venue_name=venue_name or getattr(show, "venue_name", None) or show.venue_code,
            session_id=show.session_id,
            time=getattr(show, "time", None) or show.show_time,
            show_datetime=show.show_datetime,
            format=show.format,
            availability_code=show.availability_code,
            availability_text=show.availability_text,
            seat_total=getattr(show, "seat_total", None),
            seat_available=getattr(show, "seat_available", None),
        )

    async def _enrich_seat_status(
        self,
        session: AsyncSession,
        monitor: Monitor,
        region: Region,
        states: list[ShowState],
        max_checks: int,
    ) -> list[ShowState]:
        """Enrich shows with the primary seat-layout category availability.

        The primary endpoint returns one payload per venue/date and includes
        every show at that venue, so this is much cheaper than one request per
        show. It exposes category availability, not exact seat counts.
        """
        by_venue: dict[str, list[ShowState]] = {}
        for state in states:
            by_venue.setdefault(state.venue_code, []).append(state)

        enriched: list[ShowState] = []
        checked = 0
        for venue_code, venue_states in by_venue.items():
            if checked >= max_checks:
                enriched.extend(venue_states)
                continue
            try:
                info_map = await self.client.seat_primary(
                    monitor.event_code,
                    monitor.date_code,
                    _region_ref(region),
                    venue_code,
                )
            except (BmsError, NoProxyAvailable):
                info_map = {}
            checked += 1

            for state in venue_states:
                info = info_map.get(state.session_id)
                if info is None:
                    enriched.append(state)
                    continue
                enriched.append(
                    replace(state, availability_text=effective_availability(info).value)
                )

        return enriched

    async def _persist_shows(
        self,
        session: AsyncSession,
        monitor: Monitor,
        movie: Movie,
        snapshot: ShowtimesSnapshot,
        current_states: list[ShowState],
    ) -> None:
        await MovieRepository(session).upsert(movie.event_code, snapshot.title or movie.title)

        result = await session.execute(
            select(Show).where(
                Show.event_code == monitor.event_code,
                Show.date_code == monitor.date_code,
            )
        )
        existing = {(show.venue_code, show.session_id): show for show in result.scalars()}
        current_keys = {(state.venue_code, state.session_id) for state in current_states}

        # Upsert theatres atomically before inserting shows. Monitors for
        # different dates at the same venue run concurrently, so a plain
        # "get then insert" races and the show row can hit its FK first.
        venue_names = {state.venue_code: state.venue_name for state in current_states}
        now = utcnow()
        for venue_code, venue_name in venue_names.items():
            await session.execute(
                sqlite_insert(Theatre)
                .values(
                    venue_code=venue_code,
                    name=venue_name,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["venue_code"],
                    set_={"name": venue_name, "last_seen_at": now, "updated_at": now},
                )
            )

        for state in current_states:
            show = existing.get((state.venue_code, state.session_id))
            if show is None:
                show = Show(
                    venue_code=state.venue_code,
                    event_code=monitor.event_code,
                    date_code=monitor.date_code,
                    session_id=state.session_id,
                    show_datetime=state.show_datetime,
                    show_time=state.time,
                    format=state.format,
                )
                session.add(show)
            show.show_datetime = state.show_datetime or show.show_datetime
            show.show_time = state.time
            show.format = state.format
            show.availability_code = state.availability_code
            show.availability_text = state.availability_text
            show.is_active = True
            show.last_seen_at = utcnow()
            if state.seat_total is not None:
                show.seat_total = state.seat_total
            if state.seat_available is not None:
                show.seat_available = state.seat_available

        for key, show in existing.items():
            if key not in current_keys:
                show.is_active = False

        await session.flush()


def _region_ref(region: Region) -> Any:
    from bmsbot.bms.client import RegionRef

    return RegionRef(region.code, region.slug, region.name, region.lat, region.lng)
