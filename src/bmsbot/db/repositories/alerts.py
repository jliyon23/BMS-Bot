from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.db.base import utcnow
from bmsbot.db.models import AlertEvent
from bmsbot.domain.enums import AlertStatus


class AlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        *,
        user_id: int,
        watch_id: int,
        monitor_id: int | None,
        alert_type: str,
        dedupe_key: str,
        payload: dict,
    ) -> bool:
        stmt = (
            sqlite_insert(AlertEvent)
            .values(
                user_id=user_id,
                watch_id=watch_id,
                monitor_id=monitor_id,
                alert_type=alert_type,
                dedupe_key=dedupe_key,
                payload=payload,
                status=AlertStatus.PENDING.value,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return bool(result.rowcount)

    async def pending(self, limit: int = 100) -> list[AlertEvent]:
        now = utcnow()
        result = await self.session.execute(
            select(AlertEvent)
            .where(
                AlertEvent.status == AlertStatus.PENDING.value,
                or_(AlertEvent.next_attempt_at.is_(None), AlertEvent.next_attempt_at <= now),
            )
            .order_by(AlertEvent.created_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_sent(self, alert_id: int) -> None:
        await self.session.execute(
            update(AlertEvent)
            .where(AlertEvent.id == alert_id)
            .values(status=AlertStatus.SENT.value, sent_at=utcnow(), last_error=None)
        )
        await self.session.flush()

    async def mark_failed(self, alert_id: int, error: str) -> None:
        await self.session.execute(
            update(AlertEvent)
            .where(AlertEvent.id == alert_id)
            .values(
                status=AlertStatus.FAILED.value,
                attempts=AlertEvent.attempts + 1,
                last_error=error[:1000],
            )
        )
        await self.session.flush()

    async def list_recent(self, limit: int = 100, status: str | None = None) -> list[AlertEvent]:
        stmt = select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(AlertEvent.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_pending(self) -> int:
        result = await self.session.execute(
            select(func.count(AlertEvent.id)).where(AlertEvent.status == AlertStatus.PENDING.value)
        )
        return int(result.scalar_one())

    async def count_failed_since(self, since: datetime) -> int:
        result = await self.session.execute(
            select(func.count(AlertEvent.id)).where(
                AlertEvent.status == AlertStatus.FAILED.value,
                AlertEvent.created_at >= since,
            )
        )
        return int(result.scalar_one())

    async def last_sent_at(self) -> datetime | None:
        result = await self.session.execute(
            select(func.max(AlertEvent.sent_at)).where(AlertEvent.sent_at.is_not(None))
        )
        return result.scalar_one_or_none()

    async def count_sent_since(self, since: datetime) -> int:
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count(AlertEvent.id)).where(
                AlertEvent.status == AlertStatus.SENT.value, AlertEvent.sent_at >= since
            )
        )
        return int(result.scalar_one())
