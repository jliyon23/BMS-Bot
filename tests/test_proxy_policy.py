from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bmsbot.db.base import Base, utcnow
from bmsbot.db.repositories.proxies import ProxyRepository
from bmsbot.domain.enums import ProxyStatus


async def _factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'proxy.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_rotating_proxy_survives_single_failure(tmp_path) -> None:
    engine, factory = await _factory(tmp_path)
    async with factory() as session:
        repo = ProxyRepository(session)
        proxy = await repo.upsert_candidate(
            url="http://1.2.3.4:8080",
            protocol="http",
            host="1.2.3.4",
            port=8080,
            country="IN",
            anonymity="premium",
            source="test",
            tier="premium",
            is_rotating=True,
        )
        proxy.status = ProxyStatus.ACTIVE.value
        await repo.record_failure(proxy, "http_403", rotating_threshold=3, rotating_cooldown_sec=60)
        assert proxy.status == ProxyStatus.ACTIVE.value
        assert proxy.consecutive_failures == 1

        await repo.record_failure(proxy, "http_403", rotating_threshold=3, rotating_cooldown_sec=60)
        assert proxy.status == ProxyStatus.ACTIVE.value

        await repo.record_failure(proxy, "http_403", rotating_threshold=3, rotating_cooldown_sec=60)
        assert proxy.status == ProxyStatus.COOLDOWN.value
        assert proxy.cooldown_until is not None
        assert proxy.cooldown_until > utcnow() + timedelta(seconds=30)
        await session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_static_proxy_cools_down_immediately(tmp_path) -> None:
    engine, factory = await _factory(tmp_path)
    async with factory() as session:
        repo = ProxyRepository(session)
        proxy = await repo.upsert_candidate(
            url="http://5.6.7.8:8080",
            protocol="http",
            host="5.6.7.8",
            port=8080,
            country="IN",
            anonymity="elite",
            source="test",
            tier="free",
            is_rotating=False,
        )
        proxy.status = ProxyStatus.ACTIVE.value
        await repo.record_failure(proxy, "timeout")
        assert proxy.status == ProxyStatus.COOLDOWN.value
        assert proxy.cooldown_until is not None
        await session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_success_resets_rotating_failure_streak(tmp_path) -> None:
    engine, factory = await _factory(tmp_path)
    async with factory() as session:
        repo = ProxyRepository(session)
        proxy = await repo.upsert_candidate(
            url="http://9.9.9.9:8080",
            protocol="http",
            host="9.9.9.9",
            port=8080,
            country="IN",
            anonymity="premium",
            source="test",
            tier="premium",
            is_rotating=True,
        )
        proxy.status = ProxyStatus.ACTIVE.value
        await repo.record_failure(proxy, "http_403")
        assert proxy.consecutive_failures == 1
        await repo.record_success(proxy, 1200)
        assert proxy.consecutive_failures == 0
        await session.commit()

    await engine.dispose()
