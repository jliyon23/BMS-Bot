from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.bot import keyboards, texts
from bmsbot.bot.ui import safe_edit
from bmsbot.db.models import Movie, Region, User, Watch
from bmsbot.db.repositories.watches import WatchRepository
from bmsbot.domain.enums import ALERT_TITLES, AlertType

router = Router(name="watches")


async def _monitor_state(session: AsyncSession, watch: Watch, active: bool) -> None:
    """Deactivate shared monitors only when nobody watches that movie anymore."""
    result = await session.execute(
        select(func.count(Watch.id)).where(
            Watch.region_code == watch.region_code,
            Watch.event_code == watch.event_code,
            Watch.is_active.is_(True),
        )
    )
    if int(result.scalar_one()) == 0 and not active:
        from bmsbot.db.models import Monitor

        await session.execute(
            update(Monitor)
            .where(
                Monitor.region_code == watch.region_code,
                Monitor.event_code == watch.event_code,
            )
            .values(is_active=False)
        )


@router.message(Command("watches"))
async def list_watches(message: Message, session: AsyncSession, db_user: User) -> None:
    watches = await WatchRepository(session).list_for_user(db_user.id, active_only=False)
    if not watches:
        await message.answer(texts.NO_WATCHES)
        return
    await message.answer(texts.WATCHES_HEADER)
    for watch in watches:
        movie = await session.get(Movie, watch.event_code)
        region = await session.get(Region, watch.region_code)
        title = movie.title if movie else watch.event_code
        region_name = region.name if region else watch.region_code
        status = "active" if watch.is_active else "paused"
        await message.answer(
            f"🎬 <b>{title}</b>\n📍 {region_name}\nStatus: <b>{status}</b>",
            reply_markup=keyboards.watch_actions(watch.id, watch.is_active),
        )


async def _owned_watch(
    callback: CallbackQuery, session: AsyncSession, db_user: User
) -> Watch | None:
    raw = callback.data or ""
    try:
        watch_id = int(raw.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Invalid watch", show_alert=True)
        return None
    watch = await WatchRepository(session).get(watch_id)
    if watch is None or watch.user_id != db_user.id:
        await callback.answer("Watch not found", show_alert=True)
        return None
    return watch


@router.callback_query(F.data.startswith("watch:toggle:"))
async def toggle_watch(
    callback: CallbackQuery, session: AsyncSession, db_user: User
) -> None:
    watch = await _owned_watch(callback, session, db_user)
    if watch is None:
        return
    watch.is_active = not watch.is_active
    await session.flush()
    await _monitor_state(session, watch, watch.is_active)
    if watch.is_active:
        from bmsbot.db.base import utcnow
        from bmsbot.db.models import Monitor

        await session.execute(
            update(Monitor)
            .where(
                Monitor.region_code == watch.region_code,
                Monitor.event_code == watch.event_code,
            )
            .values(is_active=True, next_run_at=utcnow(), lease_owner=None, lease_until=None)
        )
    if callback.message is not None:
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.watch_actions(watch.id, watch.is_active)
        )
    await callback.answer("Resumed" if watch.is_active else "Paused")


@router.callback_query(F.data.startswith("watch:delete:"))
async def delete_watch(
    callback: CallbackQuery, session: AsyncSession, db_user: User
) -> None:
    watch = await _owned_watch(callback, session, db_user)
    if watch is None:
        return
    watch.is_active = False
    await session.flush()
    await _monitor_state(session, watch, False)
    await WatchRepository(session).delete(watch)
    if callback.message is not None:
        await safe_edit(callback.message, texts.WATCH_REMOVED)
    await callback.answer("Removed")


@router.callback_query(F.data.startswith("watch:prefs:"))
async def show_watch_prefs(
    callback: CallbackQuery, session: AsyncSession, db_user: User
) -> None:
    watch = await _owned_watch(callback, session, db_user)
    if watch is None:
        return
    prefs = {pref.alert_type: pref.enabled for pref in watch.prefs}
    if callback.message is not None:
        await callback.message.answer(
            "🔔 <b>Choose alerts for this watch</b>",
            reply_markup=keyboards.watch_prefs_keyboard(watch.id, prefs),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("wpref:"))
async def toggle_watch_pref(
    callback: CallbackQuery, session: AsyncSession, db_user: User
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    if parts[1] == "done":
        await callback.answer("Saved")
        return
    try:
        watch_id = int(parts[1])
        alert_type = AlertType(parts[2])
    except (ValueError, KeyError):
        await callback.answer("Invalid alert", show_alert=True)
        return
    watch = await WatchRepository(session).get(watch_id)
    if watch is None or watch.user_id != db_user.id:
        await callback.answer("Watch not found", show_alert=True)
        return
    current = next(
        (pref.enabled for pref in watch.prefs if pref.alert_type == alert_type.value), False
    )
    await WatchRepository(session).update_pref(watch, alert_type, not current)
    await session.refresh(watch, attribute_names=["prefs"])
    prefs = {pref.alert_type: pref.enabled for pref in watch.prefs}
    if callback.message is not None:
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.watch_prefs_keyboard(watch.id, prefs)
        )
    await callback.answer(f"{ALERT_TITLES[alert_type]}: {'on' if not current else 'off'}")
