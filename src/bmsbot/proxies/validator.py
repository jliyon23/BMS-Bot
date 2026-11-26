from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from curl_cffi import requests as cffi

from bmsbot.config import Settings

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class ValidationResult:
    successes: int = 0
    iterations: int = 0
    latencies: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    http_statuses: list[int] = field(default_factory=list)
    exit_ip: str | None = None

    @property
    def stable(self) -> bool:
        return self.successes > 0


class ProxyValidator:
    """Validates a single proxy by repeating the same BMS canary request.

    The repetition is intentional: a proxy that answers once and dies is not
    stable enough for a 10-second monitor.
    """

    def __init__(self, settings: Settings, timeout: float = 15.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def _canary_url(self) -> str:
        date_code = datetime.now(IST).strftime("%Y%m%d")
        params = {
            "etCodes": self.settings.bms_canary_event,
            "dateCode": date_code,
            "isDesktop": "true",
            "regionCode": self.settings.bms_canary_region,
            "xLocationShared": "false",
            "memberId": "",
            "lsId": "",
            "subCode": "",
            "appCode": "WEB",
            "refEventCode": self.settings.bms_canary_event,
        }
        return f"{self.settings.bms_base_url}/api/movies-data/v5/showtimes-by-event/primary-dynamic?{urlencode(params)}"

    def _headers(self) -> dict[str, str]:
        region = self.settings.bms_canary_region
        return {
            "accept": "application/json, text/plain, */*",
            "x-region-code": region,
            "x-region-slug": "hyderabad" if region == "HYD" else region.lower(),
            "x-latitude": "17.385044",
            "x-longitude": "78.486671",
            "x-bms-id": f"1.{int(time.time() * 1000) % 900000000 + 100000000}.{int(time.time() * 1000)}",
            "x-app-code": "WEB",
            "x-platform-code": "WEB",
            "x-platform": "WEB",
            "origin": self.settings.bms_base_url,
            "referer": f"{self.settings.bms_base_url}/",
        }

    async def validate(self, proxy_url: str, iterations: int | None = None) -> ValidationResult:
        iterations = iterations or self.settings.proxy_validation_iterations
        result = ValidationResult(iterations=iterations)
        proxies = {"http": proxy_url, "https": proxy_url}

        # Exit IP is diagnostic only; do not fail the proxy if ipify is down.
        try:
            async with cffi.AsyncSession(impersonate=self.settings.impersonations[0]) as session:
                response = await session.get(
                    "https://api.ipify.org?format=json",
                    proxies=proxies,
                    timeout=min(self.timeout, 10),
                )
                if response.status_code == 200:
                    result.exit_ip = response.json().get("ip")
        except Exception:
            pass

        for _ in range(iterations):
            started = time.perf_counter()
            try:
                async with cffi.AsyncSession(impersonate=self.settings.impersonations[0]) as session:
                    response = await session.get(
                        self._canary_url(),
                        headers=self._headers(),
                        proxies=proxies,
                        timeout=self.timeout,
                    )
                latency = int((time.perf_counter() - started) * 1000)
                result.latencies.append(latency)
                result.http_statuses.append(response.status_code)
                if response.status_code == 200 and '"data"' in response.text:
                    result.successes += 1
                else:
                    result.errors.append(f"http_{response.status_code}")
            except Exception as exc:
                result.errors.append(type(exc).__name__)
            await asyncio.sleep(0.5)

        return result
