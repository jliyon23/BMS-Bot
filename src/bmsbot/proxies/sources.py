from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from bmsbot.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyCandidate:
    url: str
    protocol: str
    host: str
    port: int
    country: str | None
    anonymity: str | None
    source: str


def _is_public_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def normalize_proxy(
    raw: str,
    *,
    source: str,
    country: str | None = None,
    anonymity: str | None = None,
) -> ProxyCandidate | None:
    value = raw.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        return None
    if scheme in {"https"}:
        scheme = "http"  # HTTP CONNECT; TLS remains end-to-end
    if scheme in {"socks5h"}:
        scheme = "socks5"
    if not parsed.hostname or not _is_public_ip(parsed.hostname):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if not port or not (0 < port < 65536):
        return None
    return ProxyCandidate(
        url=f"{scheme}://{parsed.hostname}:{port}",
        protocol=scheme,
        host=parsed.hostname,
        port=port,
        country=country,
        anonymity=anonymity,
        source=source,
    )


async def _geonode(client: httpx.AsyncClient, settings: Settings, limit: int) -> list[ProxyCandidate]:
    response = await client.get(
        "https://proxylist.geonode.com/api/proxy-list",
        params={
            "limit": limit,
            "page": 1,
            "sort_by": "lastChecked",
            "sort_type": "desc",
            "country": settings.proxy_country,
            "protocols": "http,https",
        },
    )
    response.raise_for_status()
    payload = response.json()
    out: list[ProxyCandidate] = []
    for item in payload.get("data", []):
        candidate = normalize_proxy(
            f"http://{item.get('ip')}:{item.get('port')}",
            source="geonode",
            country=item.get("country"),
            anonymity=item.get("anonymityLevel"),
        )
        if candidate:
            out.append(candidate)
    return out


async def _proxifly(client: httpx.AsyncClient, settings: Settings, limit: int) -> list[ProxyCandidate]:
    response = await client.get(
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/"
        f"proxies/countries/{settings.proxy_country}/data.txt"
    )
    response.raise_for_status()
    out: list[ProxyCandidate] = []
    for line in response.text.splitlines()[:limit]:
        candidate = normalize_proxy(line, source="proxifly", country=settings.proxy_country)
        if candidate:
            out.append(candidate)
    return out


async def _proxyscrape(client: httpx.AsyncClient, settings: Settings, limit: int) -> list[ProxyCandidate]:
    response = await client.get(
        "https://api.proxyscrape.com/v4/free-proxy-list/get",
        params={
            "request": "display_proxies",
            "proxy_format": "protocolipport",
            "format": "text",
            "country": settings.proxy_country,
        },
    )
    response.raise_for_status()
    out: list[ProxyCandidate] = []
    for line in response.text.splitlines()[:limit]:
        candidate = normalize_proxy(line, source="proxyscrape", country=settings.proxy_country)
        if candidate:
            out.append(candidate)
    return out


async def fetch_all_sources(settings: Settings) -> list[ProxyCandidate]:
    """Fetch open proxy lists concurrently and return unique candidates."""
    if not settings.proxy_free_enabled:
        return []
    limit = settings.proxy_candidate_limit
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        tasks = [
            _geonode(client, settings, limit),
            _proxifly(client, settings, limit),
            _proxyscrape(client, settings, limit),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    by_url: dict[str, ProxyCandidate] = {}
    for result in results:
        if isinstance(result, Exception):
            log.warning("proxy source failed", extra={"worker": "proxy", "duration_ms": 0})
            continue
        for candidate in result:
            by_url.setdefault(candidate.url, candidate)
    log.info("fetched %d unique proxy candidates", len(by_url), extra={"worker": "proxy"})
    return list(by_url.values())
