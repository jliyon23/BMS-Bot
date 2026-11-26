from __future__ import annotations

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.base import utcnow
from bmsbot.db.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self.session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def upsert_telegram_user(
        self, telegram_id: int, username: str | None, first_name: str | None
    ) -> User:
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id, username=username, first_name=first_name)
            self.session.add(user)
        else:
            user.username = username
            user.first_name = first_name
        user.last_seen_at = utcnow()
        await self.session.flush()
        return user

    async def set_region(self, user: User, region_code: str, pin_code: str | None = None) -> None:
        user.region_code = region_code
        if pin_code:
            user.pin_code = pin_code
        await self.session.flush()

    async def list_page(
        self, limit: int = 50, offset: int = 0, query: str | None = None
    ) -> list[User]:
        stmt = select(User).order_by(User.id.desc()).limit(limit).offset(offset)
        if query:
            needle = f"%{query.strip()}%"
            stmt = stmt.where(
                or_(
                    User.username.ilike(needle),
                    User.first_name.ilike(needle),
                    cast(User.telegram_id, String).ilike(needle),
                )
            )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count(self) -> int:
        result = await self.session.execute(select(func.count(User.id)))
        return int(result.scalar_one())

    async def set_banned(self, user: User, banned: bool) -> None:
        user.is_banned = banned
        await self.session.flush()

    async def set_active(self, user: User, active: bool) -> None:
        user.is_active = active
        await self.session.flush()

    async def delete(self, user: User) -> None:
        await self.session.delete(user)
        await self.session.flush()

    async def touch(self, user: User) -> None:
        user.last_seen_at = utcnow()
        await self.session.flush()
