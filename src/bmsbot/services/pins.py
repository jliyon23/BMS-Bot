from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.bms.client import BmsClient
from bmsbot.db.models import PinCode
from bmsbot.db.repositories.regions import RegionRepository

log = logging.getLogger(__name__)

STATE_TO_CITIES: dict[str, tuple[str, ...]] = {
    "telangana": ("hyderabad",),
    "andhra pradesh": ("vijayawada", "visakhapatnam", "guntur", "tirupati", "warangal"),
    "karnataka": ("bengaluru",),
    "maharashtra": ("mumbai", "pune", "nagpur"),
    "tamil nadu": ("chennai", "coimbatore", "madurai"),
    "delhi": ("ncr",),
    "west bengal": ("kolkata",),
    "gujarat": ("ahmedabad", "surat", "vadodara"),
    "kerala": ("kochi", "thiruvananthapuram", "kozhikode"),
    "punjab": ("chandigarh", "ludhiana", "amritsar"),
    "haryana": ("ncr", "gurugram"),
    "uttar pradesh": ("lucknow", "noida", "kanpur", "varanasi"),
    "rajasthan": ("jaipur", "udaipur", "jodhpur"),
    "madhya pradesh": ("indore", "bhopal"),
    "odisha": ("bhubaneswar",),
    "assam": ("guwahati",),
    "bihar": ("patna",),
    "goa": ("panaji",),
    "chandigarh": ("chandigarh",),
}


@dataclass
class PinLookup:
    pin: str
    district: str | None
    state: str | None
    region_codes: list[str]


class PinService:
    def __init__(self, client: BmsClient) -> None:
        self.client = client

    async def lookup(self, session: AsyncSession, pin: str) -> PinLookup:
        cached = await session.get(PinCode, pin)
        if cached and cached.region_code:
            return PinLookup(pin, cached.district, cached.state, [cached.region_code])

        district, state = await self._postal_lookup(pin)
        region_repo = RegionRepository(session)
        regions = await region_repo.list_active()
        if not regions:
            # First user after a cold start: try to populate the region table.
            try:
                from bmsbot.services.regions import sync_regions

                await sync_regions(session, self.client)
                regions = await region_repo.list_active()
            except Exception:
                regions = []
        candidates: list[str] = []

        def add_matching(needle: str | None) -> None:
            if not needle:
                return
            needle = needle.lower()
            for region in regions:
                if region.code in candidates:
                    continue
                if (
                    region.name.lower() == needle
                    or region.slug.lower() == needle
                    or region.name.lower() in needle
                    or needle in region.name.lower()
                ):
                    candidates.append(region.code)

        add_matching(district)
        if not candidates and state:
            add_matching(state)
            for city in STATE_TO_CITIES.get(state.lower(), ()):
                add_matching(city)

        if district or state:
            if cached is None:
                session.add(
                    PinCode(
                        pin=pin,
                        district=district,
                        state=state,
                        region_code=candidates[0] if candidates else None,
                    )
                )
            else:
                cached.district = district
                cached.state = state
                cached.region_code = candidates[0] if candidates else None
            await session.flush()

        return PinLookup(pin, district, state, candidates[:10])

    async def _postal_lookup(self, pin: str) -> tuple[str | None, str | None]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"https://api.postalpincode.in/pincode/{pin}")
                response.raise_for_status()
                payload = response.json()
            if not payload or payload[0].get("Status") != "Success":
                return None, None
            offices = payload[0].get("PostOffice") or []
            if not offices:
                return None, None
            return offices[0].get("District"), offices[0].get("State")
        except Exception:
            log.warning("PIN lookup failed for %s", pin)
            return None, None
