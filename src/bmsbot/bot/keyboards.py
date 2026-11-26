from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bmsbot.db.models import Region
from bmsbot.domain.enums import ALERT_TITLES, DEFAULT_ALERTS, AlertType


def region_keyboard(regions: list[Region]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=region.name, callback_data=f"region:{region.code}")]
        for region in regions[:10]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def movies_keyboard(movies: list[tuple[str, str]], page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title[:60], callback_data=f"movie:{code}")]
        for code, title in movies
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Prev", callback_data=f"movies:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"movies:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def prefs_keyboard(prefs: dict[str, bool]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for alert_type in AlertType:
        enabled = prefs.get(alert_type.value, DEFAULT_ALERTS[alert_type])
        mark = "✅" if enabled else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {ALERT_TITLES[alert_type]}",
                    callback_data=f"pref:{alert_type.value}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Save subscription", callback_data="pref:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def watch_actions(watch_id: int, active: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="⏸ Pause" if active else "▶️ Resume",
                callback_data=f"watch:toggle:{watch_id}",
            ),
            InlineKeyboardButton(text="🔔 Alerts", callback_data=f"watch:prefs:{watch_id}"),
        ],
        [InlineKeyboardButton(text="🗑 Remove", callback_data=f"watch:delete:{watch_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def watch_prefs_keyboard(watch_id: int, prefs: dict[str, bool]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for alert_type in AlertType:
        enabled = prefs.get(alert_type.value, False)
        mark = "✅" if enabled else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {ALERT_TITLES[alert_type]}",
                    callback_data=f"wpref:{watch_id}:{alert_type.value}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Done", callback_data="wpref:done:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
