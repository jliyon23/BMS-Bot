from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.models import Region


class RegionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_many(self, regions: list[Region]) -> int:
        count = 0
        for incoming in regions:
            existing = await self.session.get(Region, incoming.code)
            if existing is None:
                self.session.add(incoming)
            else:
                existing.name = incoming.name
                existing.slug = incoming.slug
                existing.lat = incoming.lat
                existing.lng = incoming.lng
                existing.is_active = True
            count += 1
        await self.session.flush()
        return count

    async def get(self, code: str) -> Region | None:
        return await self.session.get(Region, code)

    async def by_slug(self, slug: str) -> Region | None:
        result = await self.session.execute(
            select(Region).where(Region.slug == slug).limit(1)
        )
        return result.scalar_one_or_none()

    async def list_active(self) -> list[Region]:
        result = await self.session.execute(
            select(Region).where(Region.is_active.is_(True)).order_by(Region.name)
        )
        return list(result.scalars())
