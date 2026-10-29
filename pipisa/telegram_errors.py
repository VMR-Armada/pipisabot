from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)

from pipisa.texts import plural

logger = logging.getLogger(__name__)

_MAX_PAUSE = 180


def describe_telegram_error(error: BaseException) -> str | None:
    if not isinstance(error, TelegramAPIError):
        return None
    original = _original_description(error)
    if isinstance(error, TelegramRetryAfter):
        seconds = max(1, int(error.retry_after))
        unit = plural(seconds, "секунду", "секунды", "секунд")
        return f"Telegram ограничил бота в этом чате на {seconds} {unit}. {original}"
    if isinstance(error, TelegramNetworkError):
        return "Не получилось связаться с Telegram. Попробуйте ещё раз чуть позже."
    if isinstance(error, TelegramServerError):
        return f"Telegram временно не отвечает. {original}"
    if isinstance(error, TelegramForbiddenError):
        return f"Telegram запретил действие. {original}"
    if isinstance(error, TelegramNotFound):
        return f"Telegram не нашёл чат или сообщение. {original}"
    if isinstance(error, TelegramBadRequest):
        return f"Telegram не принял запрос. {original}"
    return f"Telegram вернул ошибку. {original}"


def notice_with_cause(lead: str, error: BaseException) -> str:
    detail = describe_telegram_error(error)
    if detail is None:
        return lead
    return f"{lead}\n{detail}"


def delivery_target(update: object) -> tuple[int, int | None] | None:
    for attr in ("message", "edited_message", "channel_post"):
        message = getattr(update, attr, None)
        chat_id = _chat_id(message)
        if chat_id is not None:
            return chat_id, getattr(message, "message_id", None)
    query = getattr(update, "callback_query", None)
    if query is not None:
        message = getattr(query, "message", None)
        chat_id = _chat_id(message)
        if chat_id is not None:
            return chat_id, getattr(message, "message_id", None)
    for attr in ("my_chat_member", "chat_member"):
        member = getattr(update, attr, None)
        if member is None:
            continue
        chat_id = _chat_id(member)
        if chat_id is not None:
            return chat_id, None
    return None


async def tell_user(
    bot: Bot,
    chat_id: int,
    text: str,
    error: BaseException | None = None,
    *,
    reply_to: int | None = None,
) -> bool:
    wait = _pause(error) if isinstance(error, TelegramRetryAfter) else 0
    reply = reply_to
    for attempt in range(2):
        if wait:
            await asyncio.sleep(wait)
            wait = 0
        try:
            kwargs: dict[str, int] = {}
            if reply is not None:
                kwargs["reply_to_message_id"] = reply
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except TelegramRetryAfter as retry:
            if attempt == 1:
                logger.info("Не дошло сообщение об ошибке в чат %s", chat_id)
                return False
            wait = _pause(retry)
        except TelegramBadRequest:
            if reply is None or attempt == 1:
                logger.info("Не дошло сообщение об ошибке в чат %s", chat_id)
                return False
            reply = None
        except TelegramAPIError:
            logger.info("Не дошло сообщение об ошибке в чат %s", chat_id)
            return False
    return False


def _chat_id(value: object) -> int | None:
    chat = getattr(value, "chat", None)
    chat_id = getattr(chat, "id", None)
    if isinstance(chat_id, int):
        return chat_id
    return None


def _original_description(error: TelegramAPIError) -> str:
    text = error.message
    marker = "Original description: "
    if marker in text:
        text = text.split(marker, 1)[1]
    return html.escape(" ".join(text.split()))


def _pause(error: TelegramRetryAfter) -> int:
    return min(max(int(error.retry_after), 1), _MAX_PAUSE)
