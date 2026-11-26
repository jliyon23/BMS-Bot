from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.bms.client import BmsClient
from bmsbot.db.models import Region
from bmsbot.db.repositories.regions import RegionRepository

log = logging.getLogger(__name__)


async def sync_regions(session: AsyncSession, client: BmsClient) -> int:
    regions = await client.list_regions()
    rows = [
        Region(
            code=region.code,
            name=region.name,
            slug=region.slug,
            lat=region.lat,
            lng=region.lng,
        )
        for region in regions
    ]
    count = await RegionRepository(session).upsert_many(rows)
    log.info("synced %d BMS regions", count)
    return count
