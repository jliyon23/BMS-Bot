from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bmsbot.bms.client import BmsClient, BmsError, NoProxyAvailable, RegionRef
from bmsbot.bms.parsers import ShowtimesSnapshot
from bmsbot.bot import keyboards, texts
from bmsbot.bot.states import Onboarding
from bmsbot.db.base import utcnow
from bmsbot.db.models import Movie, Region, User, Watch
from bmsbot.db.repositories.monitors import MonitorRepository
from bmsbot.db.repositories.movies import MovieRepository
from bmsbot.db.repositories.regions import RegionRepository
from bmsbot.db.repositories.users import UserRepository
from bmsbot.db.repositories.watches import WatchRepository
from bmsbot.domain.enums import ALERT_TITLES, DEFAULT_ALERTS, AlertType
from bmsbot.monitor.registry import sync_watch_monitors
from bmsbot.services.formatting import ALERT_EXPLANATION
from bmsbot.services.pins import PinService

router = Router(name="onboarding")
PAGE_SIZE = 8
log = logging.getLogger(__name__)


def region_ref(region: Region) -> RegionRef:
    return RegionRef(region.code, region.slug, region.name, region.lat, region.lng)


async def load_movies(
    session: AsyncSession,
    client: BmsClient,
    region: Region,
    *,
    page: int = 0,
    refresh: bool = False,
) -> tuple[list[tuple[str, str]], int]:
    repo = MovieRepository(session)
    age = await repo.region_listing_age(region.code)
    stale = age is None or age < utcnow() - timedelta(minutes=15)
    rows = await repo.list_region(region.code, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    if refresh or stale or not rows:
        try:
            raw = await client.now_showing(region_ref(region))
        except (NoProxyAvailable, BmsError):
            raw = []
        if raw:
            await repo.replace_region_listing(
                region.code,
                [(code, title, slug, None) for code, slug, title in raw],
            )
            rows = await repo.list_region(region.code, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    total = await repo.count_region(region.code)
    return [(movie.event_code, movie.title) for movie, _ in rows], total


@router.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext,
    db_user: User,
    session: AsyncSession,
    client: BmsClient,
) -> None:
    await state.clear()
    if db_user.region_code:
        region = await RegionRepository(session).get(db_user.region_code)
        if region is not None:
            await state.set_state(Onboarding.choosing_movie)
            await state.update_data(region_code=region.code)
            await send_movie_page(message, session, client, region, page=0)
            return
    await state.set_state(Onboarding.waiting_pin)
    await message.answer(texts.WELCOME)
    await message.answer(texts.ASK_PIN)


async def send_movie_page(
    message: Message,
    session: AsyncSession,
    client: BmsClient,
    region: Region,
    *,
    page: int,
) -> None:
    movies, total = await load_movies(session, client, region, page=page)
    if not movies:
        await message.answer(texts.NO_MOVIES.format(region=region.name))
        return
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    await message.answer(
        texts.CHOOSE_MOVIE.format(region=region.name),
        reply_markup=keyboards.movies_keyboard(movies, page, total_pages),
    )


@router.message(Onboarding.waiting_pin, F.text, ~F.text.startswith("/"))
async def handle_pin(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    client: BmsClient,
) -> None:
    pin = re.sub(r"\D", "", message.text or "")
    if len(pin) != 6:
        await message.answer(texts.INVALID_PIN)
        return

    lookup = await PinService(client).lookup(session, pin)
    regions = []
    for code in lookup.region_codes:
        region = await RegionRepository(session).get(code)
        if region is not None:
            regions.append(region)

    if not regions:
        await message.answer(texts.PIN_NOT_FOUND)
        return

    await session.commit()
    await state.update_data(pin=pin, candidate_regions=[region.code for region in regions])
    await state.set_state(Onboarding.choosing_region)
    await message.answer(texts.CHOOSE_REGION, reply_markup=keyboards.region_keyboard(regions))


@router.callback_query(F.data.startswith("region:"))
async def choose_region(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    client: BmsClient,
) -> None:
    code = (callback.data or "").split(":", 1)[1]
    region = await RegionRepository(session).get(code)
    if region is None:
        await callback.answer("Region not found", show_alert=True)
        return
    await callback.answer("Loading movies...")

    data = await state.get_data()
    await UserRepository(session).set_region(db_user, region.code, data.get("pin"))
    await session.commit()
    await state.update_data(region_code=region.code)
    await state.set_state(Onboarding.choosing_movie)

    if callback.message is not None:
        await callback.message.edit_text(f"📍 {region.name}")
        await send_movie_page(callback.message, session, client, region, page=0)


@router.callback_query(F.data.startswith("movies:"))
async def page_movies(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    client: BmsClient,
) -> None:
    try:
        page = int((callback.data or "movies:0").split(":", 1)[1])
    except ValueError:
        page = 0
    await callback.answer("Loading...")
    data = await state.get_data()
    region_code = data.get("region_code") or db_user.region_code
    region = await RegionRepository(session).get(region_code) if region_code else None
    if region is None:
        await callback.answer("Set a region first with /start", show_alert=True)
        return
    movies, total = await load_movies(session, client, region, page=page)
    if callback.message is not None:
        if movies:
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            await callback.message.edit_text(
                texts.CHOOSE_MOVIE.format(region=region.name),
                reply_markup=keyboards.movies_keyboard(movies, page, total_pages),
            )
        else:
            await callback.message.edit_text(texts.NO_MOVIES.format(region=region.name))


@router.callback_query(F.data.startswith("movie:"))
async def choose_movie(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    code = (callback.data or "").split(":", 1)[1].upper()
    if not re.fullmatch(r"ET\d{6,}", code):
        await callback.answer("Invalid movie", show_alert=True)
        return
    movie = await session.get(Movie, code)
    if movie is None:
        await callback.answer("Movie not found", show_alert=True)
        return

    prefs = {alert.value: DEFAULT_ALERTS[alert] for alert in AlertType}
    await state.set_state(Onboarding.choosing_alerts)
    await state.update_data(event_code=code, title=movie.title, prefs=prefs)
    if callback.message is not None:
        await callback.message.answer(ALERT_EXPLANATION)
        await callback.message.answer(
            texts.ALERTS_HEADER.format(movie=movie.title),
            reply_markup=keyboards.prefs_keyboard(prefs),
        )
    await callback.answer()


@router.callback_query(Onboarding.choosing_alerts, F.data == "pref:done")
async def save_subscription(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    client: BmsClient,
) -> None:
    data = await state.get_data()
    event_code = data.get("event_code")
    prefs: dict[str, bool] = data.get("prefs") or {}
    if not event_code:
        await callback.answer("Start again with /start", show_alert=True)
        return
    region = await RegionRepository(session).get(db_user.region_code) if db_user.region_code else None
    if region is None:
        await callback.answer("Set a region first", show_alert=True)
        return
    await callback.answer("Saving subscription...")

    selected = {AlertType(key): bool(value) for key, value in prefs.items() if key in AlertType._value2member_map_}
    watch_repo = WatchRepository(session)
    watch = await watch_repo.get_for_user(db_user.id, region.code, event_code)
    if watch is None:
        watch = await watch_repo.create(db_user.id, region.code, event_code, prefs=selected)
    else:
        watch.is_active = True
        for alert_type, enabled in selected.items():
            await watch_repo.update_pref(watch, alert_type, enabled)
        await session.flush()

    # Commit before the BMS/date lookup so the SQLite writer lock is not held
    # across a slow proxy request.
    await session.commit()
    await _ensure_monitors(session, client, watch, region)
    # _ensure_monitors commits its own inserts. Clear FSM only after that
    # commit, otherwise the FSM write contends with the open transaction.
    if callback.message is not None:
        await callback.message.edit_text(
            texts.SUBSCRIBED.format(
                movie=data.get("title") or event_code,
                region=region.name,
            )
        )
    try:
        await state.clear()
    except Exception:
        # The subscription is already saved and confirmed; a failed FSM clear
        # should not turn into a failed Telegram update.
        log.exception("could not clear onboarding state after subscription")


async def _ensure_monitors(
    session: AsyncSession,
    client: BmsClient,
    watch: Watch,
    region: Region,
) -> None:
    """Create shared 10-second monitors, falling back to today's monitor."""
    from bmsbot.config import get_settings

    settings = get_settings()
    try:
        snapshot: ShowtimesSnapshot | None = await client.showtimes(
            watch.event_code, datetime.now().strftime("%Y%m%d"), region_ref(region)
        )
    except (NoProxyAvailable, BmsError):
        snapshot = None

    if snapshot is not None:
        try:
            await sync_watch_monitors(session, client, watch, region, settings, snapshot=snapshot)
            await session.commit()
            return
        except (NoProxyAvailable, BmsError):
            await session.rollback()

    await MonitorRepository(session).ensure(
        region.code,
        watch.event_code,
        datetime.now().strftime("%Y%m%d"),
        interval_sec=settings.poll_interval_sec,
        priority=10,
    )
    await session.commit()


@router.callback_query(Onboarding.choosing_alerts, F.data.startswith("pref:"))
async def toggle_pref(callback: CallbackQuery, state: FSMContext) -> None:
    value = (callback.data or "").split(":", 1)[1]
    try:
        alert_type = AlertType(value)
    except ValueError:
        await callback.answer("Unknown alert", show_alert=True)
        return
    data = await state.get_data()
    prefs: dict[str, bool] = data.get("prefs") or {}
    prefs[alert_type.value] = not prefs.get(alert_type.value, DEFAULT_ALERTS[alert_type])
    await state.update_data(prefs=prefs)
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=keyboards.prefs_keyboard(prefs))
    await callback.answer(f"{ALERT_TITLES[alert_type]}: {'on' if prefs[alert_type.value] else 'off'}")


@router.message(Command("movie"))
async def movie_search(
    message: Message,
    session: AsyncSession,
    db_user: User,
    client: BmsClient,
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: <code>/movie Dune Part Three</code>")
        return
    region = await RegionRepository(session).get(db_user.region_code) if db_user.region_code else None
    if region is None:
        await message.answer("Set a region first with /start or /region &lt;city&gt;.")
        return
    try:
        results = await client.search_movies(parts[1], region_ref(region))
    except (NoProxyAvailable, BmsError):
        await message.answer(texts.ERROR_NO_PROXY)
        return
    if not results:
        await message.answer(f"No movie matched <b>{parts[1]}</b>.")
        return

    repo = MovieRepository(session)
    rows: list[list[InlineKeyboardButton]] = []
    for result in results[:8]:
        await repo.upsert(result.event_code, result.title)
        rows.append(
            [InlineKeyboardButton(text=result.title[:60], callback_data=f"movie:{result.event_code}")]
        )
    await message.answer(
        "🔎 <b>Search results</b>\nSelect a movie:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled. Use /start to begin again.")
