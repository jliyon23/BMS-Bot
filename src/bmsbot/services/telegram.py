from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter

from bmsbot.services.formatting import split_message

log = logging.getLogger(__name__)


class TelegramSender:
    """Thin, testable wrapper around aiogram with throttling and splitting."""

    def __init__(self, token: str) -> None:
        self.bot = Bot(token=token) if token else None
        self._lock = asyncio.Lock()
        self._last_sent = 0.0

    @property
    def configured(self) -> bool:
        return self.bot is not None

    async def close(self) -> None:
        if self.bot is not None:
            await self.bot.session.close()

    async def send_html(self, chat_id: int, text: str) -> bool:
        if self.bot is None:
            log.warning("telegram token is not configured; message not sent")
            return False
        for chunk in split_message(text):
            try:
                async with self._lock:
                    now = asyncio.get_running_loop().time()
                    wait = 0.05 - (now - self._last_sent)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    await self.bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML)
                    self._last_sent = asyncio.get_running_loop().time()
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                await self.bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML)
            except Exception:
                log.exception("failed to send telegram message", extra={"user_id": chat_id})
                return False
        return True
