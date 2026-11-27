from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from bmsbot.config import Settings
from bmsbot.db.base import utcnow
from bmsbot.db.models import Proxy, WorkerHeartbeat
from bmsbot.db.repositories.proxies import ProxyRepository
from bmsbot.db.repositories.settings import SettingsRepository
from bmsbot.domain.enums import ProxyTier
from bmsbot.proxies.crypto import CredentialCipher, build_proxy_url
from bmsbot.proxies.sources import fetch_all_sources
from bmsbot.proxies.validator import ProxyValidator

log = logging.getLogger(__name__)


class ProxyHunterWorker:
    """Keeps the proxy pool full of proxies that answered repeatedly.

    A proxy is promoted only after N repeated BMS canaries, and active
    proxies are rechecked continuously. When a premium proxy is enabled the
    scraper ignores free proxies entirely.
    """

    def __init__(self, session_factory: async_sessionmaker, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.validator = ProxyValidator(settings)
        self._stop = asyncio.Event()

    async def _proxy_enabled(self) -> bool:
        async with self.session_factory() as session:
            return bool(await SettingsRepository(session).get("use_proxy", True))

    async def run_forever(self) -> None:
        log.info("proxy hunter started", extra={"worker": "proxy"})
        next_refresh = 0.0
        while not self._stop.is_set():
            now = asyncio.get_running_loop().time()
            try:
                if not await self._proxy_enabled():
                    # Proxy use is off: do not burn proxy-provider bandwidth on
                    # revalidation. Admin "Recheck" still validates on demand.
                    await self.heartbeat(paused=True)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=60)
                    except TimeoutError:
                        pass
                    continue
                if now >= next_refresh:
                    await self.refresh_candidates()
                    next_refresh = now + self.settings.proxy_refresh_sec
                await self.validate_round()
                await self.heartbeat()
            except Exception:
                log.exception("proxy hunter iteration failed", extra={"worker": "proxy"})
                await asyncio.sleep(15)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def refresh_candidates(self) -> int:
        if not self.settings.proxy_free_enabled:
            return 0
        async with self.session_factory() as session:
            has_premium = await ProxyRepository(session).has_enabled_premium()
        if has_premium:
            # Free proxies are ignored by the scraper in premium mode; do not
            # waste source/validation bandwidth on them.
            return 0

        candidates = await fetch_all_sources(self.settings)
        async with self.session_factory() as session:
            repository = ProxyRepository(session)
            added = 0
            for candidate in candidates:
                existing = await repository.by_url(candidate.url)
                if existing is not None:
                    continue
                await repository.upsert_candidate(
                    url=candidate.url,
                    protocol=candidate.protocol,
                    host=candidate.host,
                    port=candidate.port,
                    country=candidate.country,
                    anonymity=candidate.anonymity,
                    source=candidate.source,
                )
                added += 1
            await session.commit()
        log.info("added %d proxy candidates", added, extra={"worker": "proxy"})
        return added

    async def validate_round(self) -> None:
        cutoff = utcnow() - timedelta(seconds=self.settings.proxy_recheck_sec)
        async with self.session_factory() as session:
            repository = ProxyRepository(session)
            # Query the relevant tier directly. Otherwise a large free list can
            # fill the limit and the single premium candidate is never reached.
            has_premium = await repository.has_enabled_premium()
            tier = ProxyTier.PREMIUM.value if has_premium else None
            candidates = await repository.validation_candidates(
                limit=self.settings.proxy_validation_batch, tier=tier
            )
            due_active = await repository.active_due(
                cutoff, limit=self.settings.proxy_validation_batch, tier=tier
            )
        batch = candidates + due_active
        if has_premium:
            # Strict premium mode: never fall back to validating/using free proxies.
            batch = [proxy for proxy in batch if proxy.tier == ProxyTier.PREMIUM.value]
        if not batch:
            return

        semaphore = asyncio.Semaphore(self.settings.proxy_validation_concurrency)

        async def check(proxy: Proxy) -> None:
            async with semaphore:
                await self._validate_one(proxy.id, proxy.url)

        await asyncio.gather(*(check(proxy) for proxy in batch), return_exceptions=True)

    async def _validate_one(self, proxy_id: int, proxy_url: str) -> None:
        async with self.session_factory() as session:
            proxy = await ProxyRepository(session).get(proxy_id)
            if proxy is None:
                return
            is_rotating = proxy.is_rotating
            credentials = None
            if proxy.credentials_encrypted:
                try:
                    credentials = CredentialCipher(self.settings.proxy_encryption_key).decrypt(
                        proxy.credentials_encrypted
                    )
                except ValueError:
                    log.error("could not decrypt proxy credentials", extra={"proxy_id": proxy_id})
            effective_url = build_proxy_url(
                proxy.protocol, proxy.host, proxy.port, credentials
            )

        iterations = self.settings.proxy_validation_iterations
        result = await self.validator.validate(effective_url, iterations=iterations)
        required = (
            self.settings.proxy_validation_required_rotating
            if is_rotating
            else self.settings.proxy_validation_required
        )
        max_p95 = (
            self.settings.proxy_max_p95_ms_rotating
            if is_rotating
            else self.settings.proxy_max_p95_ms
        )
        async with self.session_factory() as session:
            repository = ProxyRepository(session)
            proxy = await repository.get(proxy_id)
            if proxy is None:
                return
            stable = await repository.record_validation(
                proxy,
                successes=result.successes,
                iterations=iterations,
                latencies=result.latencies,
                errors=result.errors,
                min_successes=required,
                max_p95_ms=max_p95,
                cooldown_sec=(
                    self.settings.rotating_cooldown_sec
                    if is_rotating
                    else 600
                ),
            )
            if result.exit_ip:
                proxy.exit_ip = result.exit_ip
            for index, latency in enumerate(result.latencies):
                await repository.record_check(
                    proxy_id,
                    iteration=index + 1,
                    ok=True,
                    latency_ms=latency,
                    http_status=200,
                    error=None,
                )
            for error in result.errors:
                await repository.record_check(
                    proxy_id,
                    iteration=0,
                    ok=False,
                    latency_ms=0,
                    http_status=None,
                    error=error,
                )
            await session.commit()
            log.info(
                "proxy %s %s (%d/%d, p95=%sms)",
                proxy.url,
                "active" if stable else "rejected",
                result.successes,
                iterations,
                proxy.p95_ms,
                extra={"proxy_id": proxy_id, "worker": "proxy"},
            )

    async def heartbeat(self, *, paused: bool = False) -> None:
        async with self.session_factory() as session:
            heartbeat = await session.get(WorkerHeartbeat, "proxy-hunter")
            if heartbeat is None:
                heartbeat = WorkerHeartbeat(name="proxy-hunter", status="ok", detail={})
                session.add(heartbeat)
            heartbeat.status = "ok"
            heartbeat.detail = {"paused": True, "reason": "use_proxy=false"} if paused else {}
            heartbeat.last_seen_at = utcnow()
            await session.commit()
