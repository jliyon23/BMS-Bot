from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from bmsbot.config import Settings
from bmsbot.db.models import Proxy
from bmsbot.db.repositories.proxies import ProxyRepository
from bmsbot.proxies.crypto import CredentialCipher, build_proxy_url

log = logging.getLogger(__name__)


class NoProxyAvailable(RuntimeError):
    """Raised when the pool cannot serve a proxy. Never fall back to direct."""


@dataclass(frozen=True)
class ProxyLease:
    proxy_id: int
    url: str
    tier: str
    is_rotating: bool = False


class StaticProxyPool:
    """Single-proxy adapter for the standalone CLI (still proxy-only)."""

    def __init__(self, url: str, is_rotating: bool = False) -> None:
        self.url = url
        self.is_rotating = is_rotating

    async def acquire(self) -> ProxyLease:
        return ProxyLease(
            proxy_id=0, url=self.url, tier="static", is_rotating=self.is_rotating
        )

    async def report_success(self, lease: ProxyLease, latency_ms: int) -> None:
        return None

    async def report_failure(self, lease: ProxyLease, error: str) -> None:
        return None


class ProxyPool:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.cipher = CredentialCipher(settings.proxy_encryption_key)
        self._rr = 0
        self._local_cooldown: dict[int, datetime] = {}
        self._premium_cache_until = 0.0
        self._premium_cached = False

    async def _has_premium(self) -> bool:
        now = time.monotonic()
        if now < self._premium_cache_until:
            return self._premium_cached
        async with self.session_factory() as session:
            value = await ProxyRepository(session).has_enabled_premium()
        self._premium_cached = value
        self._premium_cache_until = now + 10.0
        return value

    def _lease_url(self, proxy: Proxy) -> str:
        credentials = None
        if proxy.credentials_encrypted:
            credentials = self.cipher.decrypt(proxy.credentials_encrypted)
        return build_proxy_url(proxy.protocol, proxy.host, proxy.port, credentials)

    async def acquire(self) -> ProxyLease:
        premium_only = await self._has_premium()
        async with self.session_factory() as session:
            proxies = await ProxyRepository(session).active(premium_only=premium_only, limit=50)

        now = datetime.utcnow()
        usable = [
            proxy
            for proxy in proxies
            if self._local_cooldown.get(proxy.id, now) <= now
        ]
        if not usable:
            mode = "premium" if premium_only else "free"
            raise NoProxyAvailable(f"No active {mode} proxy available")

        proxy = usable[self._rr % len(usable)]
        self._rr += 1
        return ProxyLease(
            proxy_id=proxy.id,
            url=self._lease_url(proxy),
            tier=proxy.tier,
            is_rotating=proxy.is_rotating,
        )

    async def report_success(self, lease: ProxyLease, latency_ms: int) -> None:
        async with self.session_factory() as session:
            repository = ProxyRepository(session)
            proxy = await repository.get(lease.proxy_id)
            if proxy is not None:
                await repository.record_success(proxy, latency_ms)
                await session.commit()

    async def report_failure(self, lease: ProxyLease, error: str) -> None:
        cooldown = (
            timedelta(seconds=self.settings.rotating_cooldown_sec)
            if lease.is_rotating
            else timedelta(minutes=10)
        )
        self._local_cooldown[lease.proxy_id] = datetime.utcnow() + cooldown
        async with self.session_factory() as session:
            repository = ProxyRepository(session)
            proxy = await repository.get(lease.proxy_id)
            if proxy is not None:
                await repository.record_failure(
                    proxy,
                    error,
                    rotating_threshold=self.settings.rotating_failure_threshold,
                    rotating_cooldown_sec=self.settings.rotating_cooldown_sec,
                )
                await session.commit()
        log.warning("proxy failed: %s", error, extra={"proxy_id": lease.proxy_id})

    async def stats(self) -> dict[str, int]:
        async with self.session_factory() as session:
            counts = await ProxyRepository(session).counts()
            premium = await ProxyRepository(session).has_enabled_premium()
        counts["premium_mode"] = int(premium)
        return counts

    async def reset_cooldowns(self) -> None:
        """Used by the admin after adding premium proxies."""
        self._local_cooldown.clear()
        self._premium_cache_until = 0.0
        await asyncio.sleep(0)
