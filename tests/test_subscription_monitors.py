from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bmsbot.bms.parsers import ShowtimesSnapshot
from bmsbot.bot.handlers.onboarding import _ensure_monitors
from bmsbot.db.base import Base
from bmsbot.db.models import Monitor, Movie, Region, User
from bmsbot.db.repositories.watches import WatchRepository


class FakeClient:
    async def showtimes(self, event_code, date_code, region):
        return ShowtimesSnapshot(
            title="Test Movie",
            actual_date="20261127",
            available_dates=["20261127", "20261128"],
            theatres=[],
        )


@pytest.mark.asyncio
async def test_ensure_monitors_commits_before_fsm_clear(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'sub.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        session.add(Region(code="HYD", name="Hyderabad", slug="hyderabad", lat="1", lng="2"))
        session.add(Movie(event_code="ET00489093", title="Bhogi"))
        session.add(User(telegram_id=1, first_name="A"))
        await session.flush()
        watch = await WatchRepository(session).create(1, "HYD", "ET00489093")
        watch = await WatchRepository(session).get(watch.id)
        assert watch is not None
        region = await session.get(Region, "HYD")
        assert region is not None

        await _ensure_monitors(session, FakeClient(), watch, region)

    # A separate connection must already see the committed monitors.
    async with factory() as reader:
        count = (await reader.execute(select(func.count(Monitor.id)))).scalar_one()
        assert count == 2

    await engine.dispose()
