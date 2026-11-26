from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests as cffi

from bmsbot.bms.http import AsyncRateLimiter
from bmsbot.bms.parsers import (
    ParsedTheatre,
    RegionInfo,
    SearchResult,
    SeatCounts,
    ShowtimesSnapshot,
    parse_movie_links,
    parse_regions,
    parse_search_results,
    parse_seat_layout,
    parse_seat_primary,
    parse_showtimes,
)
from bmsbot.config import Settings
from bmsbot.proxies.pool import NoProxyAvailable, ProxyPool

log = logging.getLogger(__name__)


class BmsError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegionRef:
    code: str
    slug: str
    name: str
    lat: str
    lng: str


class BmsClient:
    """BookMyShow client.

    Requests go through the proxy pool by default. The admin can flip the live
    ``use_proxy`` setting for direct testing; in production direct mode is
    refused unless BMS_ALLOW_DIRECT is explicitly enabled.
    """

    def __init__(
        self,
        settings: Settings,
        pool: ProxyPool,
        limiter: AsyncRateLimiter | None = None,
        max_proxy_tries: int = 5,
        proxy_enabled: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.limiter = limiter or AsyncRateLimiter(settings.max_requests_per_sec, burst=2)
        self.max_proxy_tries = max_proxy_tries
        self.proxy_enabled = proxy_enabled
        self._regions: list[RegionInfo] | None = None
        self._direct_warned = False

    # ------------------------------------------------------------------ core

    def _headers(self, region: RegionRef | None, referer: str | None = None) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "origin": self.settings.bms_base_url,
            "x-bms-id": (
                f"1.{random.randint(100_000_000, 999_999_999)}.{int(time.time() * 1000)}"
            ),
            "x-app-code": "WEB",
            "x-platform-code": "WEB",
            "x-platform": "WEB",
        }
        if region is not None:
            headers.update(
                {
                    "x-region-code": region.code,
                    "x-region-slug": region.slug,
                    "x-latitude": region.lat,
                    "x-longitude": region.lng,
                }
            )
        if referer:
            headers["referer"] = referer
        return headers

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        region: RegionRef | None = None,
        referer: str | None = None,
        expect_json: bool = True,
        timeout: float = 25.0,
    ) -> Any:
        if self.proxy_enabled is not None and not await self.proxy_enabled():
            return await self._request_direct(
                url,
                params=params,
                region=region,
                referer=referer,
                expect_json=expect_json,
                timeout=timeout,
            )

        errors: list[str] = []
        for _ in range(self.max_proxy_tries):
            lease = await self.pool.acquire()
            # A rotating proxy mixes good and bad exit IPs. Repeated attempts
            # are expected to hit a healthy exit.
            attempts = self.settings.rotating_retries if lease.is_rotating else 1
            proxy_error: str | None = None

            for _attempt in range(attempts):
                for impersonate in self.settings.impersonations:
                    await self.limiter.acquire()
                    started = time.perf_counter()
                    try:
                        async with cffi.AsyncSession(impersonate=impersonate) as session:
                            response = await session.get(
                                url,
                                params=params,
                                headers=self._headers(region, referer),
                                proxies={"http": lease.url, "https": lease.url},
                                timeout=timeout,
                            )
                    except Exception as exc:
                        errors.append(f"{impersonate}:{type(exc).__name__}")
                        proxy_error = f"{type(exc).__name__}: {exc}"
                        continue

                    latency_ms = int((time.perf_counter() - started) * 1000)
                    if response.status_code == 200:
                        try:
                            payload = response.json() if expect_json else response.text
                        except json.JSONDecodeError:
                            errors.append(f"{impersonate}:invalid_json")
                            proxy_error = "invalid_json"
                            continue
                        await self.pool.report_success(lease, latency_ms)
                        return payload

                    if response.status_code in (403, 429, 500, 502, 503, 504):
                        errors.append(f"{impersonate}:http_{response.status_code}")
                        proxy_error = f"http_{response.status_code}"
                        if lease.is_rotating:
                            continue
                        break

                    raise BmsError(
                        f"BMS returned HTTP {response.status_code}: {response.text[:200]}"
                    )

                if not lease.is_rotating:
                    break

            if proxy_error is not None:
                await self.pool.report_failure(lease, proxy_error)

        raise BmsError("BMS request failed after proxy retries: " + ", ".join(errors[-6:]))

    async def _request_direct(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        region: RegionRef | None = None,
        referer: str | None = None,
        expect_json: bool = True,
        timeout: float = 25.0,
    ) -> Any:
        if not self.settings.direct_allowed:
            raise BmsError(
                "Direct mode is disabled in production. Set BMS_ALLOW_DIRECT=true "
                "or re-enable the proxy."
            )
        if not self._direct_warned:
            log.warning("BMS direct mode is enabled; proxies are bypassed")
            self._direct_warned = True

        errors: list[str] = []
        for impersonate in self.settings.impersonations:
            await self.limiter.acquire()
            try:
                async with cffi.AsyncSession(impersonate=impersonate) as session:
                    response = await session.get(
                        url,
                        params=params,
                        headers=self._headers(region, referer),
                        timeout=timeout,
                    )
            except Exception as exc:
                errors.append(f"{impersonate}:{type(exc).__name__}")
                continue

            if response.status_code == 200:
                if expect_json:
                    try:
                        return response.json()
                    except json.JSONDecodeError:
                        errors.append(f"{impersonate}:invalid_json")
                        continue
                return response.text

            if response.status_code in (403, 429, 500, 502, 503, 504):
                errors.append(f"{impersonate}:http_{response.status_code}")
                continue
            raise BmsError(f"BMS returned HTTP {response.status_code}: {response.text[:200]}")

        raise BmsError("Direct BMS request failed: " + ", ".join(errors[-4:]))

    # --------------------------------------------------------------- regions

    async def list_regions(self) -> list[RegionInfo]:
        if self._regions is not None:
            return self._regions
        text = await self._request(
            f"{self.settings.bms_base_url}/serv/getData?cmd=GETREGIONS",
            expect_json=False,
            timeout=30.0,
        )
        regions = parse_regions(text)
        if not regions:
            raise BmsError("Could not parse BMS region list")
        self._regions = regions
        return regions

    async def region(self, query: str) -> RegionRef:
        regions = await self.list_regions()
        needle = query.strip().lower()
        aliases = {
            "bangalore": "bengaluru",
            "bombay": "mumbai",
            "madras": "chennai",
            "calcutta": "kolkata",
            "cochin": "kochi",
            "delhi": "ncr",
            "new-delhi": "ncr",
            "delhi-ncr": "ncr",
            "gurgaon": "ncr",
            "gurugram": "ncr",
            "noida": "ncr",
        }
        needle = aliases.get(needle, needle)
        for region in regions:
            if needle in {region.slug.lower(), region.name.lower(), region.code.lower(), region.group.lower()}:
                return RegionRef(region.code, region.slug, region.name, region.lat, region.lng)
        raise BmsError(f"Unknown BMS region {query!r}")

    # ---------------------------------------------------------------- search

    async def search_movies(self, query: str, region: RegionRef) -> list[SearchResult]:
        payload = await self._request(
            f"{self.settings.bms_base_url}/api/v1/search/dynamic",
            params={"q": query},
            region=region,
            referer=f"{self.settings.bms_base_url}/",
        )
        return parse_search_results(payload)

    async def now_showing(self, region: RegionRef) -> list[tuple[str, str, str]]:
        # Delhi's BMS region slug is national-capital-region-ncr, but the
        # public explore route is /explore/movies-delhi-ncr.
        explore_slug = {
            "national-capital-region-ncr": "delhi-ncr",
        }.get(region.slug, region.slug)
        html = await self._request(
            f"{self.settings.bms_base_url}/explore/movies-{explore_slug}",
            region=region,
            referer=f"{self.settings.bms_base_url}/",
            expect_json=False,
        )
        return parse_movie_links(html)

    # ------------------------------------------------------------- showtimes

    async def showtimes(
        self,
        event_code: str,
        date_code: str,
        region: RegionRef,
        ref_event_code: str | None = None,
    ) -> ShowtimesSnapshot:
        params = {
            "etCodes": event_code,
            "dateCode": date_code,
            "isDesktop": "true",
            "regionCode": region.code,
            "xLocationShared": "false",
            "memberId": "",
            "lsId": "",
            "subCode": "",
            "appCode": "WEB",
            "refEventCode": ref_event_code or event_code,
        }
        payload = await self._request(
            f"{self.settings.bms_base_url}/api/movies-data/v5/showtimes-by-event/primary-dynamic",
            params=params,
            region=region,
            referer=f"{self.settings.bms_base_url}/explore/movies-{region.slug}",
        )
        if payload.get("error"):
            raise BmsError(f"Showtimes error: {payload['error']}")
        return parse_showtimes(payload.get("data") or {})

    async def venue_details(self, venue_code: str, region: RegionRef) -> dict[str, Any]:
        payload = await self._request(
            f"{self.settings.bms_base_url}/api/movies/v1/cinema/showcase",
            params={"vc": venue_code},
            region=region,
            referer=f"{self.settings.bms_base_url}/",
        )
        return payload.get("data") or {}

    async def seat_primary(
        self, event_code: str, date_code: str, region: RegionRef, venue_code: str
    ) -> dict[str, Any]:
        """Per-show category availability for one venue and date.

        This is the endpoint the web client uses for the primary seat layout.
        It is one request per venue (not per show) and returns the category
        availability that BMS displays before entering the booking flow.
        """
        payload = await self._request(
            f"{self.settings.bms_base_url}/api/movies-data/seatlayout/v1/primary",
            params={
                "eventCode": event_code,
                "dateCode": date_code,
                "regionCode": region.code,
                "venueCode": venue_code,
            },
            region=region,
            referer=f"{self.settings.bms_base_url}/",
        )
        return parse_seat_primary(payload)

    async def seat_layout(
        self, venue_code: str, session_id: str, event_code: str, region: RegionRef
    ) -> SeatCounts | None:
        """Try the known seat-layout routes. Returns None when unavailable."""
        # The movie seat-layout API lives under /api/movies-data/seatlayout/v1.
        # See the eventsSeatlayoutApi/moviesSeatlayoutApi config in the web client.
        seat_base = f"{self.settings.bms_base_url}/api/movies-data/seatlayout/v1"
        seat_params = {
            "eventCode": event_code,
            "venueCode": venue_code,
            "sessionID": session_id,
            "category": "",
            "appCode": "WEB",
            "appVersion": "0",
        }
        candidates = [
            (f"{seat_base}/seatLayout", seat_params),
            (
                f"{seat_base}/seatLayout/showinfo",
                {"venueCode": venue_code, "sessionID": session_id, "eventCode": event_code},
            ),
            (
                f"{seat_base}/v1/seatLayout",
                {**seat_params, "isVistaEvent": "false"},
            ),
            (
                f"{seat_base}/layoutDrawing",
                {
                    "venueID": venue_code.lower(),
                    "eventID": event_code.lower(),
                    "sessionID": session_id,
                    "filters": "seatLayout.data.base",
                },
            ),
            (
                f"{self.settings.bms_base_url}/api/seatLayout",
                seat_params,
            ),
            (
                f"{self.settings.bms_base_url}/api/seatLayout/showinfo",
                {"venueCode": venue_code, "sessionID": session_id, "eventCode": event_code},
            ),
        ]
        for url, params in candidates:
            try:
                payload = await self._request(
                    url,
                    params=params,
                    region=region,
                    referer=f"{self.settings.bms_base_url}/",
                    timeout=20.0,
                )
            except BmsError:
                continue
            counts = parse_seat_layout(payload)
            if counts is not None:
                return counts
        return None


__all__ = [
    "BmsClient",
    "BmsError",
    "NoProxyAvailable",
    "RegionRef",
    "ParsedTheatre",
    "ShowtimesSnapshot",
]
