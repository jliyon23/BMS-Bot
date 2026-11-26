from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiogram.fsm.storage.base import BaseStorage, StorageKey
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bmsbot.db.base import utcnow
from bmsbot.db.models import FSMState

log = logging.getLogger(__name__)


class SQLiteFSMStorage(BaseStorage):
    """Persistent FSM state with short write retries.

    Bot FSM writes and monitor/worker writes share one SQLite file. SQLite
    allows only one writer, so a brief "database is locked" is retried instead
    of surfacing as a failed Telegram update.
    """

    def __init__(self, session_factory: async_sessionmaker, retries: int = 6) -> None:
        self.session_factory = session_factory
        self.retries = retries

    async def _write(
        self, operation: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        for attempt in range(self.retries):
            try:
                async with self.session_factory() as session:
                    await operation(session)
                    await session.commit()
                return
            except OperationalError as exc:
                if "database is locked" not in str(exc).lower() or attempt == self.retries - 1:
                    raise
                delay = 0.2 * (attempt + 1)
                log.warning("FSM write locked; retrying in %.1fs", delay)
                await asyncio.sleep(delay)

    async def _get_row(self, key: StorageKey) -> FSMState | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(FSMState).where(
                    FSMState.bot_id == key.bot_id,
                    FSMState.chat_id == key.chat_id,
                    FSMState.user_id == key.user_id,
                )
            )
            return result.scalar_one_or_none()

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        state_value = state.state if hasattr(state, "state") else state

        async def operation(session: AsyncSession) -> None:
            result = await session.execute(
                select(FSMState).where(
                    FSMState.bot_id == key.bot_id,
                    FSMState.chat_id == key.chat_id,
                    FSMState.user_id == key.user_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(
                    FSMState(
                        bot_id=key.bot_id,
                        chat_id=key.chat_id,
                        user_id=key.user_id,
                        state=state_value,
                        data={},
                    )
                )
            else:
                row.state = state_value
                row.updated_at = utcnow()

        await self._write(operation)

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._get_row(key)
        return row.state if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = dict(data)

        async def operation(session: AsyncSession) -> None:
            result = await session.execute(
                select(FSMState).where(
                    FSMState.bot_id == key.bot_id,
                    FSMState.chat_id == key.chat_id,
                    FSMState.user_id == key.user_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(
                    FSMState(
                        bot_id=key.bot_id,
                        chat_id=key.chat_id,
                        user_id=key.user_id,
                        state=None,
                        data=payload,
                    )
                )
            else:
                row.data = payload
                row.updated_at = utcnow()

        await self._write(operation)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._get_row(key)
        return dict(row.data or {}) if row else {}

    async def close(self) -> None:
        return None
