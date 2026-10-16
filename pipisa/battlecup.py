from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from pipisa.db import Battlecup, Database
from pipisa.texts import battlecup_result, escape_name

logger = logging.getLogger(__name__)

STAKE = 2
MIN_MINUTES = 1
MAX_MINUTES = 24 * 60
BOWLING = "🎳"


def join_keyboard(battlecup_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Присоединиться", callback_data=f"cup:join:{battlecup_id}")]
        ]
    )


async def watch_battlecups(bot: Bot, db: Database) -> None:
    await db.requeue_running_battlecups(int(time.time()))
    while True:
        try:
            due = await db.claim_due_battlecups(int(time.time()))
            for cup in due:
                await play_battlecup(bot, db, cup)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Не удалось проверить баттлкапы")
        await asyncio.sleep(1)


async def play_battlecup(bot: Bot, db: Database, cup: Battlecup) -> None:
    if len(cup.players) < 2:
        refunded = await db.refund_battlecup(cup.id)
        text = "Не набралось двоих. Взносы вернулись." if refunded and refunded.players else "Не набралось двоих."
        await _edit_or_send(bot, cup, text)
        return
    await _edit_or_send(bot, cup, "🏆 Набор закрыт. Бросаем шары.")
    try:
        rolls: dict[int, int] = {}
        for player in cup.players:
            name = escape_name(player.first_name, player.username, player.user_id)
            await bot.send_message(cup.chat_id, f"Шар {name}")
            thrown = await bot.send_dice(cup.chat_id, emoji=BOWLING)
            if thrown.dice is None:
                raise RuntimeError("Telegram не вернул результат боулинга")
            rolls[player.user_id] = thrown.dice.value
        await asyncio.sleep(4)
        outcome = await db.finish_battlecup(cup.id, rolls)
    except Exception:
        logger.exception("Не удалось доиграть баттлкап %s", cup.id)
        refunded = await db.refund_battlecup(cup.id)
        if refunded is not None:
            await bot.send_message(cup.chat_id, "Броски не удались. Взносы вернулись.")
        return
    if outcome is None:
        await bot.send_message(cup.chat_id, "Этот баттлкап уже посчитан.")
        return
    await bot.send_message(cup.chat_id, battlecup_result(outcome))


async def _edit_or_send(bot: Bot, cup: Battlecup, text: str) -> None:
    if cup.message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=cup.chat_id,
                message_id=cup.message_id,
                reply_markup=None,
            )
            return
        except TelegramBadRequest:
            logger.info("Не смог обновить сообщение баттлкапа %s", cup.id)
    await bot.send_message(cup.chat_id, text)
