from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)


async def safe_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit a message, tolerating repeated taps that change nothing.

    Telegram rejects an edit when the text and markup are byte-identical, which
    used to surface as an unhandled ``TelegramBadRequest`` and abort the handler.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return False
        log.warning("could not edit telegram message: %s", exc)
        return False
