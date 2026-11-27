from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.bms.client import BmsClient, BmsError, NoProxyAvailable
from bmsbot.bot import texts
from bmsbot.bot.handlers.onboarding import send_movie_page
from bmsbot.bot.ui import safe_edit
from bmsbot.db.models import Region, User
from bmsbot.db.repositories.regions import RegionRepository
from bmsbot.db.repositories.users import UserRepository
from bmsbot.services.formatting import ALERT_EXPLANATION
from bmsbot.services.regions import sync_regions

router = Router(name="settings")


def _delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Delete everything", callback_data="delete:confirm"),
                InlineKeyboardButton(text="Cancel", callback_data="delete:cancel"),
            ]
        ]
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(texts.HELP)
    await message.answer(ALERT_EXPLANATION)


@router.message(Command("settings"))
async def settings_command(message: Message, session: AsyncSession, db_user: User) -> None:
    region = await session.get(Region, db_user.region_code) if db_user.region_code else None
    await message.answer(
        texts.SETTINGS.format(
            region=region.name if region else "not set",
            pin=db_user.pin_code or "not set",
        )
    )


@router.message(Command("region"))
async def set_region(
    message: Message,
    session: AsyncSession,
    db_user: User,
    client: BmsClient,
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: <code>/region hyderabad</code>")
        return
    try:
        ref = await client.region(parts[1])
    except (BmsError, NoProxyAvailable):
        await message.answer(texts.ERROR_NO_PROXY)
        return

    region = await RegionRepository(session).get(ref.code)
    if region is None:
        try:
            await sync_regions(session, client)
        except (BmsError, NoProxyAvailable):
            await message.answer(texts.ERROR_NO_PROXY)
            return
        region = await RegionRepository(session).get(ref.code)
    if region is None:
        await message.answer("Region not found.")
        return

    await UserRepository(session).set_region(db_user, region.code, db_user.pin_code)
    await session.commit()
    await message.answer(f"📍 Region set to <b>{region.name}</b>.")
    await send_movie_page(message, session, client, region, page=0)


@router.message(Command("movies"))
async def movies_command(
    message: Message,
    session: AsyncSession,
    db_user: User,
    client: BmsClient,
) -> None:
    region = await session.get(Region, db_user.region_code) if db_user.region_code else None
    if region is None:
        await message.answer("Set a region first with /start or /region &lt;city&gt;.")
        return
    await send_movie_page(message, session, client, region, page=0)


@router.message(Command("delete"))
async def delete_account(message: Message) -> None:
    await message.answer(
        "⚠️ This removes your account, watches and preferences. Continue?",
        reply_markup=_delete_keyboard(),
    )


@router.callback_query(F.data == "delete:confirm")
async def confirm_delete(
    callback: CallbackQuery, session: AsyncSession, db_user: User
) -> None:
    await UserRepository(session).delete(db_user)
    from bmsbot.db.repositories.monitors import MonitorRepository

    await MonitorRepository(session).deactivate_orphans()
    if callback.message is not None:
        await safe_edit(callback.message, texts.DELETED)
    await callback.answer("Deleted")


@router.callback_query(F.data == "delete:cancel")
async def cancel_delete(callback: CallbackQuery) -> None:
    if callback.message is not None:
        await safe_edit(callback.message, "Cancelled. Your data is unchanged.")
    await callback.answer()
