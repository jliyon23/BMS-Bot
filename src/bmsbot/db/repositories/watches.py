from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bmsbot.db.models import Movie, Region, Watch, WatchAlertPref
from bmsbot.domain.enums import DEFAULT_ALERTS, AlertType


class WatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, watch_id: int) -> Watch | None:
        result = await self.session.execute(
            select(Watch).options(selectinload(Watch.prefs)).where(Watch.id == watch_id)
        )
        return result.scalar_one_or_none()

    async def get_for_user(self, user_id: int, region_code: str, event_code: str) -> Watch | None:
        result = await self.session.execute(
            select(Watch)
            .options(selectinload(Watch.prefs))
            .where(
                Watch.user_id == user_id,
                Watch.region_code == region_code,
                Watch.event_code == event_code,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self, user_id: int, region_code: str, event_code: str, prefs: dict[AlertType, bool] | None = None
    ) -> Watch:
        watch = Watch(user_id=user_id, region_code=region_code, event_code=event_code)
        self.session.add(watch)
        await self.session.flush()
        selected = prefs or DEFAULT_ALERTS
        for alert_type in AlertType:
            self.session.add(
                WatchAlertPref(
                    watch_id=watch.id,
                    alert_type=alert_type.value,
                    enabled=selected.get(alert_type, DEFAULT_ALERTS[alert_type]),
                )
            )
        await self.session.flush()
        await self.session.refresh(watch, attribute_names=["prefs"])
        return watch

    async def list_for_user(self, user_id: int, active_only: bool = True) -> list[Watch]:
        stmt = (
            select(Watch)
            .options(selectinload(Watch.prefs))
            .where(Watch.user_id == user_id)
            .order_by(Watch.id.desc())
        )
        if active_only:
            stmt = stmt.where(Watch.is_active.is_(True))
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_all_active(self) -> list[Watch]:
        result = await self.session.execute(
            select(Watch)
            .options(selectinload(Watch.prefs))
            .where(Watch.is_active.is_(True))
            .order_by(Watch.id)
        )
        return list(result.scalars())

    async def list_active_for_event(self, region_code: str, event_code: str) -> list[Watch]:
        result = await self.session.execute(
            select(Watch)
            .options(selectinload(Watch.prefs))
            .where(
                Watch.is_active.is_(True),
                Watch.region_code == region_code,
                Watch.event_code == event_code,
            )
        )
        return list(result.scalars())

    async def set_active(self, watch: Watch, active: bool) -> None:
        watch.is_active = active
        await self.session.flush()

    async def update_pref(self, watch: Watch, alert_type: AlertType, enabled: bool) -> None:
        for pref in watch.prefs:
            if pref.alert_type == alert_type.value:
                pref.enabled = enabled
                await self.session.flush()
                return
        self.session.add(
            WatchAlertPref(watch_id=watch.id, alert_type=alert_type.value, enabled=enabled)
        )
        await self.session.flush()

    async def delete(self, watch: Watch) -> None:
        await self.session.delete(watch)
        await self.session.flush()

    async def list_page(self, limit: int = 50, offset: int = 0) -> list[tuple[Watch, Movie, Region]]:
        result = await self.session.execute(
            select(Watch, Movie, Region)
            .join(Movie, Movie.event_code == Watch.event_code)
            .join(Region, Region.code == Watch.region_code)
            .order_by(Watch.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def count_active(self) -> int:
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count(Watch.id)).where(Watch.is_active.is_(True))
        )
        return int(result.scalar_one())
