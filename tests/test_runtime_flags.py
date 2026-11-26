from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bmsbot.db.base import Base
from bmsbot.services.runtime import RuntimeFlags


@pytest.mark.asyncio
async def test_use_proxy_toggle_round_trip(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'flags.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    flags = RuntimeFlags(factory, ttl_sec=0)
    assert await flags.use_proxy() is True
    await flags.set_use_proxy(False)
    assert await flags.use_proxy() is False
    await flags.set_use_proxy(True)
    assert await flags.use_proxy() is True

    await engine.dispose()
