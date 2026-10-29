from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from pipisa.db import Battlecup, Database
from pipisa.texts import battlecup_announce, battlecup_result, battlecup_round, escape_name

logger = logging.getLogger(__name__)

STAKE = 2
MIN_MINUTES = 1
MAX_MINUTES = 24 * 60
DEFAULT_MINUTES = 10
THROWS = 5
BOWLING = "🎳"
THROW_GAP = 1
DICE_PAUSE = 4


def parse_battlecup_minutes(args: str | None) -> int | None:
    token = (args or "").strip()
    if not token:
        return DEFAULT_MINUTES
    if not token.isdigit() or len(token) > 4:
        return None
    minutes = int(token)
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        return None
    return minutes


def countdown_seconds(starts_at: int, now: int) -> int:
    remaining = starts_at - now
    if remaining <= 0:
        return 0
    return ((remaining + 59) // 60) * 60


def announcement_text(cup: Battlecup, now: int) -> str:
    names = [escape_name(player.first_name, player.username, player.user_id) for player in cup.players]
    return battlecup_announce(
        starts_in=countdown_seconds(cup.starts_at, now),
        stake=cup.stake,
        names=names,
    )


def join_keyboard(battlecup_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Присоединиться", callback_data=f"cup:join:{battlecup_id}")]
        ]
    )


async def watch_battlecups(bot: Bot, db: Database) -> None:
    await db.requeue_running_battlecups(int(time.time()))
    shown_minute: dict[int, int] = {}
    while True:
        try:
            now = int(time.time())
            await refresh_countdowns(bot, db, shown_minute, now)
            due = await db.claim_due_battlecups(now)
            for cup in due:
                shown_minute.pop(cup.id, None)
                await play_battlecup(bot, db, cup)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Не удалось проверить баттлкапы")
        await asyncio.sleep(1)


async def refresh_countdowns(bot: Bot, db: Database, shown_minute: dict[int, int], now: int) -> None:
    for cup in await db.pending_battlecups():
        if cup.message_id is None or cup.starts_at <= now:
            continue
        minute = countdown_seconds(cup.starts_at, now) // 60
        if shown_minute.get(cup.id) == minute:
            continue
        shown_minute[cup.id] = minute
        try:
            await bot.edit_message_text(
                announcement_text(cup, now),
                chat_id=cup.chat_id,
                message_id=cup.message_id,
                reply_markup=join_keyboard(cup.id),
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                logger.info("Не смог обновить таймер баттлкапа %s", cup.id)


async def play_battlecup(bot: Bot, db: Database, cup: Battlecup) -> None:
    if len(cup.players) < 2:
        refunded = await db.refund_battlecup(cup.id)
        text = "Не набралось двоих. Взносы вернулись." if refunded and refunded.players else "Не набралось двоих."
        await _edit_or_send(bot, cup, text)
        return
    await _edit_or_send(bot, cup, "🏆 Набор закрыт. Бросаем шары.")
    scoreboard_id: int | None = None
    pending_ids: list[int] = []
    try:
        totals = {player.user_id: 0 for player in cup.players}
        for index in range(1, THROWS + 1):
            pending_ids = []
            round_rolls: dict[int, int] = {}
            header = await bot.send_message(cup.chat_id, f"🎳 Бросок {index} из {THROWS}")
            pending_ids.append(header.message_id)
            for offset, player in enumerate(cup.players):
                if offset:
                    await asyncio.sleep(THROW_GAP)
                name = escape_name(player.first_name, player.username, player.user_id)
                label = await bot.send_message(cup.chat_id, name)
                thrown = await bot.send_dice(cup.chat_id, emoji=BOWLING)
                if thrown.dice is None:
                    raise RuntimeError("Telegram не вернул результат боулинга")
                round_rolls[player.user_id] = thrown.dice.value
                pending_ids.extend((label.message_id, thrown.message_id))
            await asyncio.sleep(DICE_PAUSE)
            for user_id, value in round_rolls.items():
                totals[user_id] += value
            rows = [
                (
                    escape_name(player.first_name, player.username, player.user_id),
                    round_rolls[player.user_id],
                    totals[player.user_id],
                )
                for player in cup.players
            ]
            rows.sort(key=lambda item: (-item[2], item[0]))
            text = battlecup_round(throw_index=index, throw_count=THROWS, rows=rows)
            scoreboard_id = await _show_scoreboard(bot, cup.chat_id, scoreboard_id, text)
            await _delete_messages(bot, cup.chat_id, pending_ids)
            pending_ids = []
        outcome = await db.finish_battlecup(cup.id, totals)
    except Exception:
        logger.exception("Не удалось доиграть баттлкап %s", cup.id)
        await _delete_messages(bot, cup.chat_id, pending_ids)
        refunded = await db.refund_battlecup(cup.id)
        if refunded is not None:
            await bot.send_message(cup.chat_id, "Броски не удались. Взносы вернулись.")
        return
    if outcome is None:
        await bot.send_message(cup.chat_id, "Этот баттлкап уже посчитан.")
        return
    await bot.send_message(cup.chat_id, battlecup_result(outcome))


async def _show_scoreboard(bot: Bot, chat_id: int, message_id: int | None, text: str) -> int:
    if message_id is not None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
            return message_id
        except TelegramBadRequest:
            logger.info("Не смог обновить промежуточный итог баттлкапа")
    sent = await bot.send_message(chat_id, text)
    return sent.message_id


async def _delete_messages(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            logger.info("Не смог удалить бросок %s в чате %s", message_id, chat_id)


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
