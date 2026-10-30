from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, ExceptionTypeFilter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
    ErrorEvent,
)

from pipisa.battlecup import (
    MAX_MINUTES,
    MIN_MINUTES,
    STAKE,
    announcement_text,
    join_keyboard,
    parse_battlecup_minutes,
)
from pipisa.cleanup import CLEAR_MAX_MINUTES, CLEAR_MIN_MINUTES
from pipisa.db import Database, Entry, Standing
from pipisa.stats_card import CardData, render_stats
from pipisa.telegram_errors import delivery_target, describe_telegram_error, notice_with_cause, tell_user
from pipisa.top_card import render_top
from pipisa.texts import (
    GROUP_ONLY,
    HELP_TEXT,
    PRIVATE_WELCOME,
    ffa_challenge,
    clear_disabled,
    clear_enabled,
    clear_status,
    cooldown_message,
    display_name,
    duel_challenge,
    duel_refusal,
    duel_result,
    escape_name,
    format_wait,
    grow_message,
    review_text,
    split_html,
    stats_fallback,
    top_message,
)

logger = logging.getLogger(__name__)
router = Router()

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


def _is_group(message: Message) -> bool:
    return message.chat.type in GROUP_TYPES


def _person(message: Message) -> tuple[int, str | None, str]:
    user = message.from_user
    assert user is not None
    return user.id, user.username, user.first_name or "Игрок"


def _rows(
    entries: list[Entry],
    with_chat: bool,
    *,
    show_username: bool = True,
) -> list[tuple[int, str, int, str | None]]:
    rows = []
    for entry in entries:
        note = entry.chat_title if with_chat else None
        username = entry.username if show_username else None
        rows.append(
            (
                entry.user_id,
                escape_name(entry.first_name, username, entry.user_id),
                entry.size,
                note,
            )
        )
    return rows


def _standing(standing: Standing | None) -> tuple[int, int, int] | None:
    if standing is None:
        return None
    return standing.rank, standing.total, standing.size


async def _claim(message: Message, db: Database) -> None:
    if message.from_user is None:
        return
    user_id, username, first_name = _person(message)
    await db.claim_import(
        user_id=user_id,
        chat_id=message.chat.id,
        username=username,
        first_name=first_name,
        chat_title=message.chat.title,
    )


def _target_user(message: Message) -> User | None:
    reply = message.reply_to_message
    if reply is None or reply.from_user is None or reply.from_user.is_bot:
        return None
    return reply.from_user


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(HELP_TEXT)
        return
    await message.answer(PRIVATE_WELCOME)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("dick"))
async def cmd_dick(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    user_id, username, first_name = _person(message)
    await _claim(message, db)
    result = await db.try_grow(
        user_id=user_id,
        chat_id=message.chat.id,
        username=username,
        first_name=first_name,
        chat_title=message.chat.title,
    )
    if not result.ok:
        text = cooldown_message(
            first_name=first_name,
            username=username,
            user_id=user_id,
            size=result.size,
            wait_seconds=result.wait_seconds,
        )
    else:
        text = grow_message(
            first_name=first_name,
            username=username,
            user_id=user_id,
            delta=result.delta,
            size=result.size,
            wait_seconds=result.wait_seconds,
        )
    await message.reply(text)


@router.message(Command("top_dick"))
async def cmd_top(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    await db.touch_chat(message.chat.id, message.chat.title)
    await _claim(message, db)
    user_id, username, first_name = _person(message)
    entries, standing = await db.chat_top(
        message.chat.id,
        user_id,
        username=username,
        first_name=first_name,
    )
    await message.reply(top_message("Топ 10 этого чата", _rows(entries, with_chat=False), _standing(standing)))


@router.message(Command("top10_pics"))
async def cmd_top_pics(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    await db.touch_chat(message.chat.id, message.chat.title)
    await _claim(message, db)
    user_id, username, first_name = _person(message)
    entries, standing = await db.chat_top(
        message.chat.id,
        user_id,
        username=username,
        first_name=first_name,
    )
    if not entries:
        await message.reply("Пока пусто. Первый замер — команда /dick.")
        return
    try:
        png = render_top([(entry.first_name or "Игрок", entry.size) for entry in entries])
    except Exception:
        logger.exception("Не удалось собрать картинку топа")
        await message.reply(top_message("Топ 10 этого чата", _rows(entries, with_chat=False), _standing(standing)))
        return
    await message.reply_photo(BufferedInputFile(png, filename="top.png"))


@router.message(Command("global_top"))
async def cmd_global(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if _is_group(message):
        await db.touch_chat(message.chat.id, message.chat.title)
        await _claim(message, db)
    entries, standing = await db.global_top(
        message.from_user.id,
        message.chat.id if _is_group(message) else None,
    )
    await message.reply(
        top_message(
            "Глобальный топ 10",
            _rows(entries, with_chat=True, show_username=False),
            _standing(standing),
        )
    )


@router.message(Command("global_top_pic"))
async def cmd_global_top_pic(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if _is_group(message):
        await db.touch_chat(message.chat.id, message.chat.title)
        await _claim(message, db)
    entries, standing = await db.global_top(
        message.from_user.id,
        message.chat.id if _is_group(message) else None,
    )
    if not entries:
        await message.reply("Пока пусто. Первый замер — команда /dick.")
        return
    try:
        png = render_top(
            [(entry.first_name or "Игрок", entry.size) for entry in entries],
            title="ГЛОБАЛЬНЫЙ ТОП",
        )
    except Exception:
        logger.exception("Не удалось собрать картинку глобального топа")
        await message.reply(
            top_message(
                "Глобальный топ 10",
                _rows(entries, with_chat=True, show_username=False),
                _standing(standing),
            )
        )
        return
    await message.reply_photo(BufferedInputFile(png, filename="global_top.png"))


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    user_id, username, first_name = _person(message)
    await db.touch_chat(message.chat.id, message.chat.title)
    await _claim(message, db)
    stats = await db.get_stats(user_id, message.chat.id)
    if stats is None:
        await message.reply("Сначала сделай замер: /dick")
        return
    name = display_name(stats.first_name or first_name, stats.username or username, user_id)
    now = int(time.time())
    if stats.next_free_at is None or now >= stats.next_free_at:
        free_text = "Можно сделать замер."
    else:
        free_text = f"Следующий замер через {format_wait(stats.next_free_at - now)}."
    try:
        png = render_stats(
            CardData(
                name=name,
                chat_title=message.chat.title or stats.chat_title,
                size=stats.size,
                rank=stats.rank,
                total=stats.total,
                plays=stats.plays,
                best_gain=stats.best_gain,
                worst_loss=stats.worst_loss,
                next_free_at=stats.next_free_at,
                now=now,
                deltas=stats.deltas,
                duel_wins=stats.duel_wins,
                duel_losses=stats.duel_losses,
                duel_ties=stats.duel_ties,
                cup_wins=stats.cup_wins,
                cup_played=stats.cup_played,
                contests=tuple(stats.contests),
                rivals=tuple(stats.rivals),
            )
        )
    except Exception:
        logger.exception("Не удалось собрать карточку статистики")
        await message.reply(
            stats_fallback(
                name=name,
                size=stats.size,
                rank=stats.rank,
                total=stats.total,
                plays=stats.plays,
                best_gain=stats.best_gain,
                worst_loss=stats.worst_loss,
                duel_wins=stats.duel_wins,
                duel_losses=stats.duel_losses,
                duel_ties=stats.duel_ties,
                cup_wins=stats.cup_wins,
                cup_played=stats.cup_played,
                rivals=stats.rivals,
                next_free_text=free_text,
            )
        )
        return
    await message.reply_photo(
        BufferedInputFile(png, filename="stats.png"),
        caption=free_text,
    )


@router.message(Command("day_review"))
async def cmd_day_review(message: Message, db: Database) -> None:
    await _send_review(message, db, week=False)


@router.message(Command("week_review"))
async def cmd_week_review(message: Message, db: Database) -> None:
    await _send_review(message, db, week=True)


async def _send_review(message: Message, db: Database, *, week: bool) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    await db.touch_chat(message.chat.id, message.chat.title)
    await _claim(message, db)
    review = await db.chat_review(message.chat.id, week=week)
    parts = split_html(review_text(review, week=week, zone=db.zone))
    await message.reply(parts[0])
    for part in parts[1:]:
        await message.answer(part)


_NICK = re.compile(r"^@?([A-Za-z0-9_]{4,})$")


def _duel_username(args: str | None) -> str | None:
    token = (args or "").strip().split(maxsplit=1)[0] if args else ""
    match = _NICK.fullmatch(token)
    if match is None:
        return None
    return match.group(1)


def _duel_keyboard(duel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Принять", callback_data=f"duel:ok:{duel_id}"),
                InlineKeyboardButton(text="Отказаться", callback_data=f"duel:no:{duel_id}"),
            ],
        ]
    )


@router.message(Command("duel"))
async def cmd_duel(message: Message, command: CommandObject, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    challenger_id, challenger_username, challenger_first = _person(message)
    challenger = await db.find_player(challenger_id, message.chat.id)
    if challenger is None:
        await message.reply("Сначала сделай /dick, чтобы было что ставить на дуэль.")
        return
    target = _target_user(message)
    if target is not None:
        if target.id == challenger_id:
            await message.reply("Себе вызвать нельзя.")
            return
        opponent = await db.find_player(target.id, message.chat.id)
        if opponent is None:
            name = escape_name(target.first_name, target.username, target.id)
            await message.reply(
                f"Не знаю {name} в этом чате. Соперник должен хотя бы раз сделать /dick."
            )
            return
    else:
        username = _duel_username(command.args)
        if username is None:
            await message.reply(
                "Ответь командой /duel на сообщение соперника или напиши ник: /duel @nickname"
            )
            return
        if challenger_username and username.lower() == challenger_username.lower():
            await message.reply("Себе вызвать нельзя.")
            return
        opponent = await db.find_player_by_username(message.chat.id, username)
        if opponent is None:
            await message.reply(
                f"Не знаю @{html.escape(username)} в этом чате. "
                "Соперник должен хотя бы раз сделать /dick."
            )
            return
        if opponent.user_id == challenger_id:
            await message.reply("Себе вызвать нельзя.")
            return
    duel_id = await db.create_duel(
        chat_id=message.chat.id,
        challenger_id=challenger_id,
        opponent_id=opponent.user_id,
    )
    if duel_id is None:
        existing = await db.find_open_duel(message.chat.id, challenger_id, opponent.user_id)
        if (
            existing is not None
            and existing.status == "pending"
            and existing.challenger_id == challenger_id
        ):
            sent = await message.reply(
                "Этот вызов ещё ждёт ответа. От него можно отказаться.",
                reply_markup=_duel_keyboard(existing.id),
            )
            await db.hold_message(sent.chat.id, sent.message_id, kind="duel", ref_id=existing.id)
            return
        await message.reply("Между вами уже есть неотвеченный вызов.")
        return
    sent = await message.reply(
        duel_challenge(
            escape_name(challenger_first, challenger_username, challenger_id),
            escape_name(opponent.first_name, opponent.username, opponent.user_id),
        ),
        reply_markup=_duel_keyboard(duel_id),
    )
    await db.hold_message(sent.chat.id, sent.message_id, kind="duel", ref_id=duel_id)


@router.message(Command("ffa"))
async def cmd_ffa(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    challenger_id, challenger_username, challenger_first = _person(message)
    challenger = await db.find_player(challenger_id, message.chat.id)
    if challenger is None:
        await message.reply("Сначала сделай /dick, чтобы было что ставить на дуэль.")
        return
    duel_id = await db.create_ffa_duel(chat_id=message.chat.id, challenger_id=challenger_id)
    if duel_id is None:
        existing = await db.find_pending_ffa(message.chat.id, challenger_id)
        if existing is None:
            await message.reply("Открытый вызов уже есть.")
            return
        sent = await message.reply(
            "Этот вызов ещё ждёт желающего. От него можно отказаться.",
            reply_markup=_duel_keyboard(existing.id),
        )
        await db.hold_message(sent.chat.id, sent.message_id, kind="duel", ref_id=existing.id)
        return
    sent = await message.reply(
        ffa_challenge(escape_name(challenger_first, challenger_username, challenger_id)),
        reply_markup=_duel_keyboard(duel_id),
    )
    await db.hold_message(sent.chat.id, sent.message_id, kind="duel", ref_id=duel_id)


@router.callback_query(F.data.startswith("duel:"))
async def cb_duel(query: CallbackQuery, db: Database) -> None:
    if query.from_user is None or query.data is None or not isinstance(query.message, Message):
        return
    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] not in {"ok", "no", "cancel"}:
        await query.answer("Не понял кнопку.", show_alert=True)
        return
    try:
        duel_id = int(parts[2])
    except ValueError:
        await query.answer("Не понял кнопку.", show_alert=True)
        return
    duel = await db.get_duel(duel_id)
    if duel is None or query.message.chat.id != duel.chat_id:
        await query.answer("Дуэль не найдена.", show_alert=True)
        return
    if parts[1] in {"no", "cancel"} and query.from_user.id == duel.challenger_id:
        cancelled = await db.cancel_duel(duel_id, query.from_user.id)
        if not cancelled:
            await query.answer("Эта дуэль уже закрыта.", show_alert=True)
            return
        await query.answer()
        challenger = escape_name(duel.challenger_name, duel.challenger_username, duel.challenger_id)
        await query.message.edit_text(duel_refusal(challenger, challenger=True), reply_markup=None)
        return
    if parts[1] == "cancel":
        await query.answer("Отказаться можно кнопкой «Отказаться».", show_alert=True)
        return
    if duel.opponent_id is None:
        if parts[1] == "no":
            await query.answer("Отменить открытый вызов может только его автор.", show_alert=True)
            return
        if query.from_user.id == duel.challenger_id:
            await query.answer("Свой вызов принять нельзя.", show_alert=True)
            return
        player = await db.find_player(query.from_user.id, duel.chat_id)
        if player is None:
            await query.answer("Сначала сделай /dick, чтобы было что ставить.", show_alert=True)
            return
        busy = await db.find_open_duel(duel.chat_id, duel.challenger_id, query.from_user.id)
        if busy is not None:
            await query.answer("Между вами уже есть неотвеченный вызов.", show_alert=True)
            return
        claimed = await db.claim_duel(duel_id, query.from_user.id)
        if not claimed:
            await query.answer("Этот вызов уже разобрали.", show_alert=True)
            return
        await query.answer()
        await _play_duel(query.message, db, duel_id)
        return
    if query.from_user.id != duel.opponent_id:
        await query.answer("Ответить может только тот, кого вызвали.", show_alert=True)
        return
    if parts[1] == "no":
        declined = await db.decline_duel(duel_id, query.from_user.id)
        if not declined:
            await query.answer("Эта дуэль уже закрыта.", show_alert=True)
            return
        await query.answer()
        opponent = escape_name(duel.opponent_name, duel.opponent_username, duel.opponent_id)
        await query.message.edit_text(duel_refusal(opponent, challenger=False), reply_markup=None)
        return

    claimed = await db.claim_duel(duel_id, query.from_user.id)
    if not claimed:
        await query.answer("Эта дуэль уже закрыта.", show_alert=True)
        return
    await query.answer()
    await _play_duel(query.message, db, duel_id)


async def _play_duel(message: Message, db: Database, duel_id: int) -> None:
    duel = await db.get_duel(duel_id)
    if duel is None or duel.opponent_id is None:
        await message.answer("Дуэль не найдена.")
        return
    challenger = escape_name(duel.challenger_name, duel.challenger_username, duel.challenger_id)
    opponent = escape_name(duel.opponent_name, duel.opponent_username, duel.opponent_id)
    await message.edit_text(
        f"{challenger} против {opponent}. Бросаем кубики.",
        reply_markup=None,
    )
    try:
        await message.answer(f"Кубик {challenger}")
        first = await message.answer_dice(emoji="🎲")
        await message.answer(f"Кубик {opponent}")
        second = await message.answer_dice(emoji="🎲")
        if first.dice is None or second.dice is None:
            raise RuntimeError("Telegram не вернул значение кубика")
        await asyncio.sleep(4)
        outcome = await db.apply_duel(duel_id, first.dice.value, second.dice.value)
    except Exception as error:
        logger.exception("Не удалось доиграть дуэль %s", duel_id)
        await db.release_duel(duel_id)
        if message.bot is None:
            return
        await tell_user(
            message.bot,
            message.chat.id,
            notice_with_cause("Кубики не бросились. Вызов снова можно принять.", error),
            error,
            reply_to=message.message_id,
        )
        return
    if outcome is None:
        await message.answer("Эта дуэль уже посчитана.")
        return
    await message.answer(duel_result(outcome))


_MINUTES = re.compile(r"^\d{1,4}$")


def _battlecup_minutes(args: str | None) -> int | None:
    token = (args or "").strip()
    if not _MINUTES.fullmatch(token):
        return None
    return int(token)


async def _refresh_battlecup(message: Message, db: Database, battlecup_id: int) -> None:
    cup = await db.get_battlecup(battlecup_id)
    if cup is None or cup.status != "pending":
        return
    try:
        await message.edit_text(announcement_text(cup, int(time.time())), reply_markup=join_keyboard(battlecup_id))
    except TelegramBadRequest:
        logger.info("Не смог обновить набор баттлкапа %s", battlecup_id)


@router.message(Command("battlecup"))
async def cmd_battlecup(message: Message, command: CommandObject, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    minutes = parse_battlecup_minutes(command.args)
    if minutes is None:
        await message.reply(
            f"Напиши длительность в минутах, от {MIN_MINUTES} до {MAX_MINUTES}: /battlecup 5. "
            "Без числа старт через 10 минут."
        )
        return
    starts_at = int(time.time()) + minutes * 60
    battlecup_id = await db.create_battlecup(chat_id=message.chat.id, stake=STAKE, starts_at=starts_at)
    if battlecup_id is None:
        await message.reply("В этом чате уже есть баттлкап.")
        return
    cup = await db.get_battlecup(battlecup_id)
    if cup is None:
        await message.reply("Не удалось открыть баттлкап.")
        return
    sent = await message.reply(
        announcement_text(cup, int(time.time())),
        reply_markup=join_keyboard(battlecup_id),
    )
    await db.set_battlecup_message(battlecup_id, sent.message_id)
    await db.hold_message(sent.chat.id, sent.message_id, kind="battlecup", ref_id=battlecup_id)


@router.message(Command("clear"))
async def cmd_clear(message: Message, command: CommandObject, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    token = (command.args or "").strip()
    if not token:
        await message.reply(clear_status(await db.get_clear_minutes(message.chat.id)))
        return
    minutes = _battlecup_minutes(token)
    if minutes is None or minutes > CLEAR_MAX_MINUTES:
        await message.reply(
            f"Напиши минуты от {CLEAR_MIN_MINUTES} до {CLEAR_MAX_MINUTES}: /clear 20. Ноль выключает очистку."
        )
        return
    if minutes < CLEAR_MIN_MINUTES:
        await db.set_clear_minutes(message.chat.id, None)
        await message.reply(clear_disabled())
        return
    await db.set_clear_minutes(message.chat.id, minutes)
    await message.reply(clear_enabled(minutes))


@router.callback_query(F.data.startswith("cup:join:"))
async def cb_battlecup_join(query: CallbackQuery, db: Database) -> None:
    if query.from_user is None or query.from_user.is_bot or query.data is None:
        return
    if not isinstance(query.message, Message):
        await query.answer("Сообщение уже недоступно.", show_alert=True)
        return
    try:
        battlecup_id = int(query.data.removeprefix("cup:join:"))
    except ValueError:
        await query.answer("Не понял кнопку.", show_alert=True)
        return
    result = await db.join_battlecup(battlecup_id, query.from_user.id)
    if result == "unknown":
        await query.answer("Сначала сделай /dick, чтобы было что ставить.", show_alert=True)
        return
    if result == "already":
        await query.answer("Ты уже в игре.", show_alert=True)
        return
    if result == "closed":
        await query.answer("Набор уже закрыт.", show_alert=True)
        return
    await query.answer("Взнос принят.")
    await _refresh_battlecup(query.message, db, battlecup_id)


@router.my_chat_member()
async def bot_added(event: ChatMemberUpdated, bot: Bot) -> None:
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    joined = old in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} and new in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.RESTRICTED,
    }
    if not joined or event.chat.type not in GROUP_TYPES:
        return
    try:
        await bot.send_message(event.chat.id, HELP_TEXT)
    except TelegramAPIError as error:
        logger.info("Не смог поздороваться в чате %s", event.chat.id)
        await tell_user(bot, event.chat.id, notice_with_cause(HELP_TEXT, error), error)


@router.error(ExceptionTypeFilter(TelegramAPIError))
async def on_telegram_error(event: ErrorEvent, bot: Bot) -> None:
    notice = describe_telegram_error(event.exception)
    target = delivery_target(event.update)
    logger.warning("Команда получила ошибку Telegram: %s", event.exception)
    if notice is None or target is None:
        return
    chat_id, reply_to = target
    await tell_user(bot, chat_id, notice, event.exception, reply_to=reply_to)
