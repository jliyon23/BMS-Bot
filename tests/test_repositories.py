from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bmsbot.db.base import Base
from bmsbot.db.models import Movie, Region, User
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.db.repositories.watches import WatchRepository
from bmsbot.domain.enums import AlertType


async def _factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'repo.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_orphan_monitors_are_paused(tmp_path) -> None:
    engine, factory = await _factory(tmp_path)
    async with factory() as session:
        session.add(Region(code="HYD", name="Hyderabad", slug="hyderabad", lat="1", lng="2"))
        session.add(Movie(event_code="ET00489093", title="Bhogi"))
        session.add(User(telegram_id=1, first_name="A"))
        session.add(User(telegram_id=2, first_name="B"))
        await session.flush()

        watch = await WatchRepository(session).create(1, "HYD", "ET00489093")
        monitor = await MonitorRepository(session).ensure("HYD", "ET00489093", "20261127")
        await session.commit()

        await WatchRepository(session).set_active(watch, False)
        await MonitorRepository(session).deactivate_orphans()
        await session.commit()
        assert (await session.get(type(monitor), monitor.id)).is_active is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_watch_pref_toggle_persists(tmp_path) -> None:
    engine, factory = await _factory(tmp_path)
    async with factory() as session:
        session.add(Region(code="HYD", name="Hyderabad", slug="hyderabad", lat="1", lng="2"))
        session.add(Movie(event_code="ET00489093", title="Bhogi"))
        session.add(User(telegram_id=1, first_name="A"))
        await session.flush()
        watch = await WatchRepository(session).create(1, "HYD", "ET00489093")
        await WatchRepository(session).update_pref(watch, AlertType.SEAT_COUNT, True)
        await session.refresh(watch, attribute_names=["prefs"])
        values = {pref.alert_type: pref.enabled for pref in watch.prefs}
        assert values[AlertType.SEAT_COUNT.value] is True
        await session.commit()

    await engine.dispose()
