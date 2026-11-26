from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.base import utcnow
from bmsbot.db.models import Proxy, ProxyCheck
from bmsbot.domain.enums import ProxyStatus, ProxyTier


class ProxyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, proxy_id: int) -> Proxy | None:
        return await self.session.get(Proxy, proxy_id)

    async def by_url(self, url: str) -> Proxy | None:
        result = await self.session.execute(select(Proxy).where(Proxy.url == url))
        return result.scalar_one_or_none()

    async def upsert_candidate(
        self,
        *,
        url: str,
        protocol: str,
        host: str,
        port: int,
        country: str | None,
        anonymity: str | None,
        source: str,
        tier: str = ProxyTier.FREE.value,
        is_rotating: bool = False,
    ) -> Proxy:
        proxy = await self.by_url(url)
        if proxy is None:
            proxy = Proxy(
                url=url,
                protocol=protocol,
                host=host,
                port=port,
                country=country,
                anonymity=anonymity,
                source=source,
                tier=tier,
                is_rotating=is_rotating,
            )
            self.session.add(proxy)
            await self.session.flush()
        else:
            proxy.is_rotating = is_rotating
        return proxy

    async def has_enabled_premium(self) -> bool:
        result = await self.session.execute(
            select(func.count(Proxy.id)).where(
                Proxy.tier == ProxyTier.PREMIUM.value,
                Proxy.is_enabled.is_(True),
            )
        )
        return int(result.scalar_one()) > 0

    async def active(self, premium_only: bool = False, limit: int = 100) -> list[Proxy]:
        now = utcnow()
        stmt = (
            select(Proxy)
            .where(
                Proxy.is_enabled.is_(True),
                Proxy.status == ProxyStatus.ACTIVE.value,
                (Proxy.cooldown_until.is_(None)) | (Proxy.cooldown_until < now),
            )
            .order_by(Proxy.p95_ms.asc(), Proxy.success_rate.desc())
            .limit(limit)
        )
        if premium_only:
            stmt = stmt.where(Proxy.tier == ProxyTier.PREMIUM.value)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def best(self, premium_only: bool = False) -> Proxy | None:
        proxies = await self.active(premium_only=premium_only, limit=1)
        return proxies[0] if proxies else None

    async def validation_candidates(
        self,
        limit: int = 100,
        *,
        tier: str | None = None,
    ) -> list[Proxy]:
        now = utcnow()
        stmt = (
            select(Proxy)
            .where(
                Proxy.is_enabled.is_(True),
                or_(
                    Proxy.status.in_(
                        [
                            ProxyStatus.CANDIDATE.value,
                            ProxyStatus.COOLDOWN.value,
                            ProxyStatus.DEAD.value,
                        ]
                    ),
                    (
                        (Proxy.status == ProxyStatus.ACTIVE.value)
                        & (Proxy.last_checked_at.is_(None))
                    ),
                ),
                or_(Proxy.cooldown_until.is_(None), Proxy.cooldown_until < now),
            )
            .order_by(
                Proxy.last_checked_at.is_(None).desc(),
                Proxy.last_checked_at.asc(),
            )
            .limit(limit)
        )
        if tier:
            stmt = stmt.where(Proxy.tier == tier)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def active_due(
        self,
        recheck_before: datetime,
        limit: int = 100,
        *,
        tier: str | None = None,
    ) -> list[Proxy]:
        stmt = (
            select(Proxy)
            .where(
                Proxy.is_enabled.is_(True),
                Proxy.status == ProxyStatus.ACTIVE.value,
                or_(Proxy.last_checked_at.is_(None), Proxy.last_checked_at < recheck_before),
            )
            .order_by(Proxy.last_checked_at.asc())
            .limit(limit)
        )
        if tier:
            stmt = stmt.where(Proxy.tier == tier)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def record_validation(
        self,
        proxy: Proxy,
        *,
        successes: int,
        iterations: int,
        latencies: list[int],
        errors: list[str],
        min_successes: int,
        max_p95_ms: int,
        cooldown_sec: int = 600,
    ) -> bool:
        now = utcnow()
        proxy.validation_rounds = iterations
        proxy.validation_successes = successes
        proxy.last_checked_at = now
        if latencies:
            ordered = sorted(latencies)
            proxy.p50_ms = ordered[len(ordered) // 2]
            proxy.p95_ms = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
            proxy.latency_ms = ordered[-1]
        proxy.success_count += successes
        proxy.fail_count += iterations - successes
        total = proxy.success_count + proxy.fail_count
        proxy.success_rate = (proxy.success_count / total) if total else 0.0
        if successes:
            proxy.consecutive_success += 1
            proxy.last_success_at = now
        else:
            proxy.consecutive_success = 0

        stable = successes >= min_successes and (proxy.p95_ms or 0) <= max_p95_ms
        if stable:
            proxy.status = ProxyStatus.ACTIVE.value
            proxy.cooldown_until = None
        else:
            proxy.status = ProxyStatus.CANDIDATE.value
            proxy.cooldown_until = now + timedelta(seconds=cooldown_sec)
            proxy.last_error = errors[0][:500] if errors else "validation failed"

        self.session.add(ProxyCheck(proxy_id=proxy.id, ok=stable, latency_ms=proxy.p95_ms))
        await self.session.flush()
        return stable

    async def record_check(
        self, proxy_id: int, *, iteration: int, ok: bool, latency_ms: int, http_status: int | None, error: str | None
    ) -> None:
        self.session.add(
            ProxyCheck(
                proxy_id=proxy_id,
                iteration=iteration,
                ok=ok,
                latency_ms=latency_ms,
                http_status=http_status,
                error_type=error,
            )
        )
        await self.session.flush()

    async def record_success(self, proxy: Proxy, latency_ms: int) -> None:
        proxy.success_count += 1
        proxy.consecutive_success += 1
        proxy.consecutive_failures = 0
        proxy.last_success_at = utcnow()
        proxy.last_used_at = utcnow()
        proxy.latency_ms = latency_ms
        proxy.p50_ms = proxy.p50_ms or latency_ms
        proxy.p95_ms = max(proxy.p95_ms, latency_ms)
        total = proxy.success_count + proxy.fail_count
        proxy.success_rate = proxy.success_count / total if total else 1.0
        await self.session.flush()

    async def record_failure(
        self,
        proxy: Proxy,
        error: str,
        *,
        rotating_threshold: int = 3,
        rotating_cooldown_sec: int = 60,
    ) -> None:
        proxy.fail_count += 1
        proxy.consecutive_success = 0
        proxy.consecutive_failures += 1
        proxy.last_used_at = utcnow()
        proxy.last_error = error[:500]
        if proxy.is_rotating:
            # A rotating proxy mixes good and bad exit IPs. One bad exit is
            # not a proxy failure; only a run of complete failures is.
            if proxy.consecutive_failures >= rotating_threshold:
                proxy.cooldown_until = utcnow() + timedelta(seconds=rotating_cooldown_sec)
                proxy.status = ProxyStatus.COOLDOWN.value
            else:
                proxy.status = ProxyStatus.ACTIVE.value
        else:
            proxy.cooldown_until = utcnow() + timedelta(minutes=10)
            proxy.status = ProxyStatus.COOLDOWN.value
        total = proxy.success_count + proxy.fail_count
        proxy.success_rate = proxy.success_count / total if total else 0.0
        await self.session.flush()

    async def set_enabled(self, proxy: Proxy, enabled: bool) -> None:
        proxy.is_enabled = enabled
        if not enabled:
            proxy.status = ProxyStatus.DISABLED.value
        await self.session.flush()

    async def add_premium(
        self, *, url: str, label: str | None = None, is_rotating: bool = False
    ) -> Proxy:
        protocol = url.split("://", 1)[0] if "://" in url else "http"
        host_port = url.split("://", 1)[-1]
        host, _, port_text = host_port.rpartition(":")
        proxy = await self.upsert_candidate(
            url=url,
            protocol=protocol,
            host=host,
            port=int(port_text or 0),
            country=None,
            anonymity="premium",
            source=label or "admin",
            tier=ProxyTier.PREMIUM.value,
            is_rotating=is_rotating,
        )
        proxy.is_enabled = True
        proxy.status = ProxyStatus.CANDIDATE.value
        await self.session.flush()
        return proxy

    async def list_all(self, limit: int = 200, offset: int = 0) -> list[Proxy]:
        result = await self.session.execute(
            select(Proxy)
            .order_by(Proxy.tier.desc(), Proxy.status, Proxy.p95_ms)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def counts(self) -> dict[str, int]:
        result = await self.session.execute(
            select(Proxy.status, func.count(Proxy.id)).group_by(Proxy.status)
        )
        return {status: int(count) for status, count in result.all()}

    async def delete(self, proxy: Proxy) -> None:
        await self.session.delete(proxy)
        await self.session.flush()
