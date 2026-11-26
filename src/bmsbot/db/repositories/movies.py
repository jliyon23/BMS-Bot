from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.base import utcnow
from bmsbot.db.models import Movie, RegionMovie


class MovieRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, event_code: str, title: str, **fields: str | None) -> Movie:
        movie = await self.session.get(Movie, event_code)
        if movie is None:
            movie = Movie(event_code=event_code, title=title, **fields)
            self.session.add(movie)
        else:
            movie.title = title
            for key, value in fields.items():
                if value is not None:
                    setattr(movie, key, value)
        movie.last_seen_at = utcnow()
        await self.session.flush()
        return movie

    async def get(self, event_code: str) -> Movie | None:
        return await self.session.get(Movie, event_code)

    async def replace_region_listing(
        self, region_code: str, movies: list[tuple[str, str, str | None, str | None]]
    ) -> None:
        await self.session.execute(delete(RegionMovie).where(RegionMovie.region_code == region_code))
        for rank, (event_code, title, slug, poster) in enumerate(movies):
            await self.upsert(event_code, title, slug=slug, poster_url=poster)
            self.session.add(
                RegionMovie(
                    region_code=region_code,
                    event_code=event_code,
                    rank=rank,
                    fetched_at=utcnow(),
                )
            )
        await self.session.flush()

    async def list_region(
        self, region_code: str, limit: int = 50, offset: int = 0
    ) -> list[tuple[Movie, datetime]]:
        result = await self.session.execute(
            select(Movie, RegionMovie.fetched_at)
            .join(RegionMovie, RegionMovie.event_code == Movie.event_code)
            .where(RegionMovie.region_code == region_code)
            .order_by(RegionMovie.rank)
            .limit(limit)
            .offset(offset)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def count_region(self, region_code: str) -> int:
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count(RegionMovie.id)).where(RegionMovie.region_code == region_code)
        )
        return int(result.scalar_one())

    async def region_listing_age(self, region_code: str) -> datetime | None:
        result = await self.session.execute(
            select(RegionMovie.fetched_at)
            .where(RegionMovie.region_code == region_code)
            .order_by(RegionMovie.fetched_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
