from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import async_sessionmaker

from bmsbot.db.repositories.settings import SettingsRepository


class RuntimeFlags:
    """Small cached view of live admin settings shared by all workers."""

    USE_PROXY_KEY = "use_proxy"

    def __init__(self, session_factory: async_sessionmaker, ttl_sec: float = 5.0) -> None:
        self.session_factory = session_factory
        self.ttl_sec = ttl_sec
        self._value = True
        self._expires_at = 0.0

    async def use_proxy(self) -> bool:
        now = time.monotonic()
        if now < self._expires_at:
            return self._value
        async with self.session_factory() as session:
            value = await SettingsRepository(session).get(self.USE_PROXY_KEY, True)
        self._value = bool(value)
        self._expires_at = now + self.ttl_sec
        return self._value

    async def set_use_proxy(self, value: bool) -> None:
        async with self.session_factory() as session:
            await SettingsRepository(session).set(self.USE_PROXY_KEY, bool(value))
            await session.commit()
        self._value = bool(value)
        self._expires_at = time.monotonic() + self.ttl_sec

    def invalidate(self) -> None:
        self._expires_at = 0.0
