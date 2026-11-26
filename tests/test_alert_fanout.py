from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bmsbot.db.base import Base
from bmsbot.db.models import Movie, Region, User
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.db.repositories.watches import WatchRepository
from bmsbot.domain.enums import AlertType
from bmsbot.monitor.alerts import enqueue_changes
from bmsbot.monitor.diff import Change


@pytest.mark.asyncio
async def test_enqueue_fans_out_once_per_watch(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        session.add(Region(code="HYD", name="Hyderabad", slug="hyderabad", lat="17.3", lng="78.4"))
        session.add(Movie(event_code="ET00489093", title="Bhogi"))
        session.add(User(telegram_id=1001, first_name="Test"))
        await session.flush()

        watch = await WatchRepository(session).create(1001, "HYD", "ET00489093")
        monitor = await MonitorRepository(session).ensure("HYD", "ET00489093", "20261127")

        changes = [
            Change(
                alert_type=AlertType.NEW_THEATRE,
                dedupe_fragment="venue:PRHN",
                payload={"venue_code": "PRHN", "venue_name": "Prasads", "shows": []},
            )
        ]
        first = await enqueue_changes(
            session,
            monitor=monitor,
            date_code="20261127",
            movie_title="Bhogi",
            region_name="Hyderabad",
            changes=changes,
        )
        second = await enqueue_changes(
            session,
            monitor=monitor,
            date_code="20261127",
            movie_title="Bhogi",
            region_name="Hyderabad",
            changes=changes,
        )
        await session.commit()

        assert first == 1
        assert second == 0
        assert watch.id is not None

    await engine.dispose()
