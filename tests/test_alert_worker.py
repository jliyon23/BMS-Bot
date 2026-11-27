from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bmsbot.db.base import Base, utcnow
from bmsbot.db.models import Movie, Region, User
from bmsbot.db.repositories.alerts import AlertRepository
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.db.repositories.watches import WatchRepository
from bmsbot.workers.alert_worker import AlertWorker


async def _build(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'alerts.db'}")
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
        alert_repo = AlertRepository(session)
        await alert_repo.enqueue(
            user_id=watch.user_id,
            watch_id=watch.id,
            monitor_id=monitor.id,
            alert_type="new_show",
            dedupe_key="watch:1:show:1",
            payload={},
        )
        await session.commit()
    return engine, factory


def _worker(factory, *, attempts: int = 2, ttl: float = 300.0) -> AlertWorker:
    settings = SimpleNamespace(alert_max_attempts=attempts, alert_retry_ttl_sec=ttl)
    sender = SimpleNamespace(configured=True, send_html=None)
    return AlertWorker(factory, settings, sender)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_transient_failure_is_rescheduled_not_dropped(tmp_path) -> None:
    engine, factory = await _build(tmp_path)
    worker = _worker(factory)
    async with factory() as session:
        pending = await AlertRepository(session).pending()
        assert len(pending) == 1
        alert = pending[0]
        await worker._handle_failure([alert], "telegram send failed", retryable=True)
        await session.refresh(alert)
        assert alert.status == "pending"
        assert alert.attempts == 1
        assert alert.next_attempt_at is not None
        assert alert.next_attempt_at > utcnow()
        # Backoff hides it from the next poll.
        assert await AlertRepository(session).pending() == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_failure_gives_up_after_max_attempts(tmp_path) -> None:
    engine, factory = await _build(tmp_path)
    worker = _worker(factory, attempts=2)
    async with factory() as session:
        alert = (await AlertRepository(session).pending())[0]
        await worker._handle_failure([alert], "boom", retryable=True)
        await session.refresh(alert)
        await worker._handle_failure([alert], "boom", retryable=True)
        await session.refresh(alert)
        assert alert.status == "failed"
        assert alert.attempts == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_alert_is_not_retried(tmp_path) -> None:
    engine, factory = await _build(tmp_path)
    worker = _worker(factory, ttl=0.0)
    async with factory() as session:
        alert = (await AlertRepository(session).pending())[0]
        alert.created_at = utcnow() - timedelta(seconds=5)
        await session.commit()
        await worker._handle_failure([alert], "boom", retryable=True)
        await session.refresh(alert)
        assert alert.status == "failed"
    await engine.dispose()
