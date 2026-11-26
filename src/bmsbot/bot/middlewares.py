from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import async_sessionmaker

from bmsbot.db.repositories.users import UserRepository


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)
        session = data["session"]
        user = await UserRepository(session).upsert_telegram_user(
            tg_user.id, tg_user.username, tg_user.first_name
        )
        # Release SQLite's write lock before a handler performs network I/O.
        await session.commit()
        if user.is_banned:
            if isinstance(event, Message):
                await event.answer("🚫 Your access to this bot has been disabled.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Access disabled.", show_alert=True)
            return None
        data["db_user"] = user
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, limit: int = 20, window_sec: int = 60) -> None:
        self.limit = limit
        self.window_sec = window_sec
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)
        now = time.monotonic()
        bucket = self._hits[tg_user.id]
        while bucket and now - bucket[0] > self.window_sec:
            bucket.popleft()
        if len(bucket) >= self.limit:
            if isinstance(event, CallbackQuery):
                await event.answer("Too many requests. Please slow down.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Too many requests. Please slow down.")
            return None
        bucket.append(now)
        return await handler(event, data)
