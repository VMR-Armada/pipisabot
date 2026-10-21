from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.methods import SendDice, SendMessage, SendPhoto, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Message

from pipisa.db import Database

logger = logging.getLogger(__name__)

CLEAR_MIN_MINUTES = 1
CLEAR_MAX_MINUTES = 24 * 60
_TICK_SECONDS = 30
_TRACKED = (SendMessage, SendPhoto, SendDice)


class TrackBotMessages(BaseRequestMiddleware):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> TelegramType:
        result = await make_request(bot, method)
        if isinstance(method, _TRACKED) and isinstance(result, Message) and result.chat.id < 0:
            try:
                await self._db.track_message(result.chat.id, result.message_id)
            except Exception:
                logger.exception("Не удалось запомнить сообщение %s", result.message_id)
        return result


async def watch_cleanup(bot: Bot, db: Database) -> None:
    while True:
        try:
            await _delete_due(bot, db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Не удалось почистить сообщения")
        await asyncio.sleep(_TICK_SECONDS)


async def _delete_due(bot: Bot, db: Database) -> None:
    for chat_id, message_id in await db.due_bot_messages(int(time.time())):
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramNetworkError:
            logger.info("Не достучался, чтобы удалить сообщение %s в чате %s", message_id, chat_id)
            continue
        except TelegramAPIError:
            logger.info("Не смог удалить сообщение %s в чате %s", message_id, chat_id)
        await db.forget_message(chat_id, message_id)
