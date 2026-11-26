from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.models import Setting


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str, default: Any = None) -> Any:
        setting = await self.session.get(Setting, key)
        return setting.value if setting is not None else default

    async def set(self, key: str, value: Any) -> None:
        setting = await self.session.get(Setting, key)
        if setting is None:
            self.session.add(Setting(key=key, value=value))
        else:
            setting.value = value
        await self.session.flush()

    async def all(self) -> dict[str, Any]:
        result = await self.session.execute(select(Setting))
        return {row.key: row.value for row in result.scalars()}
