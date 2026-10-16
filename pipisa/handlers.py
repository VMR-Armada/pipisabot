from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from pipisa.db import Database, Entry, Standing
from pipisa.stats_card import CardData, render_stats
from pipisa.texts import (
    ADMIN_ONLY,
    GROUP_ONLY,
    HELP_TEXT,
    IMPORT_USAGE,
    PRIVATE_WELCOME,
    cooldown_message,
    display_name,
    duel_challenge,
    duel_result,
    escape_name,
    format_cm,
    format_wait,
    grow_message,
    plural,
    stats_fallback,
    top_message,
)
from pipisa.transfer import ImportItem, command_body, parse_import_text, parse_size

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


async def _is_admin(message: Message, bot: Bot) -> bool:
    if message.from_user is None:
        return False
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except TelegramBadRequest:
        return False
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


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


def _import_source(message: Message) -> str:
    own = command_body(message.text or "")
    if own:
        return own
    reply = message.reply_to_message
    if reply is not None and reply.text:
        return reply.text
    return ""


def _import_report(applied: list[ImportItem], pending: list[ImportItem], broken: list[str]) -> str:
    lines = [f"Записал {_count(len(applied) + len(pending), 'размер', 'размера', 'размеров')}."]
    if applied:
        lines.append("")
        lines.append("Уже привязаны к игрокам в этом чате:")
        lines.extend(_import_line(item) for item in applied)
    if pending:
        lines.append("")
        lines.append("В топе уже видны и привяжутся, когда человек напишет команду:")
        lines.extend(_import_line(item) for item in pending)
    if broken:
        lines.append("")
        lines.append("Эти строки не разобрал:")
        lines.extend(f"• {html.escape(line)}" for line in broken[:10])
    lines.append("")
    lines.append("Следующий /dick прибавит новый замер к записанному размеру.")
    return "\n".join(lines)


def _import_line(item: ImportItem) -> str:
    if item.username:
        label = f"{item.first_name} (@{item.username})"
    else:
        label = item.first_name
    return f"• {html.escape(label)} — {format_cm(item.size)}"


def _count(value: int, one: str, few: str, many: str) -> str:
    return f"{value} {plural(value, one, few, many)}"


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


@router.message(Command("global_top"))
async def cmd_global(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if _is_group(message):
        await db.touch_chat(message.chat.id, message.chat.title)
        await _claim(message, db)
    entries, standing = await db.global_top(message.from_user.id)
    await message.reply(
        top_message(
            "Глобальный топ 10",
            _rows(entries, with_chat=True, show_username=False),
            _standing(standing),
        )
    )


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
                next_free_text=free_text,
            )
        )
        return
    await message.reply_photo(
        BufferedInputFile(png, filename="stats.png"),
        caption=free_text,
    )


@router.message(Command("import"))
async def cmd_import(message: Message, db: Database, bot: Bot) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    if not await _is_admin(message, bot):
        await message.reply(ADMIN_ONLY)
        return
    source = _import_source(message)
    if not source:
        await message.reply(IMPORT_USAGE)
        return
    items, broken = parse_import_text(source)
    if not items:
        await message.reply("В списке не нашёл ни одного размера.\n\n" + IMPORT_USAGE)
        return
    applied, pending = await db.import_sizes(
        chat_id=message.chat.id,
        chat_title=message.chat.title,
        items=items,
    )
    await message.reply(_import_report(applied, pending, broken))


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
            ]
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
    username = _duel_username(command.args)
    if username is None:
        await message.reply("Напиши ник соперника: /duel @nickname")
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
        await message.reply("Между вами уже есть неотвеченный вызов.")
        return
    await message.reply(
        duel_challenge(
            escape_name(challenger_first, challenger_username, challenger_id),
            escape_name(opponent.first_name, opponent.username, opponent.user_id),
        ),
        reply_markup=_duel_keyboard(duel_id),
    )


@router.callback_query(F.data.startswith("duel:"))
async def cb_duel(query: CallbackQuery, db: Database) -> None:
    if query.from_user is None or query.data is None or not isinstance(query.message, Message):
        return
    parts = query.data.split(":")
    if len(parts) != 3 or parts[1] not in {"ok", "no"}:
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
        await query.message.edit_text(f"{opponent} отказался от дуэли.", reply_markup=None)
        return

    claimed = await db.claim_duel(duel_id, query.from_user.id)
    if not claimed:
        await query.answer("Эта дуэль уже закрыта.", show_alert=True)
        return
    await query.answer()
    challenger = escape_name(duel.challenger_name, duel.challenger_username, duel.challenger_id)
    opponent = escape_name(duel.opponent_name, duel.opponent_username, duel.opponent_id)
    await query.message.edit_text(
        f"{challenger} против {opponent}. Бросаем кубики.",
        reply_markup=None,
    )
    try:
        await query.message.answer(f"Кубик {challenger}")
        first = await query.message.answer_dice(emoji="🎲")
        await query.message.answer(f"Кубик {opponent}")
        second = await query.message.answer_dice(emoji="🎲")
        if first.dice is None or second.dice is None:
            raise RuntimeError("Telegram не вернул значение кубика")
        await asyncio.sleep(4)
        outcome = await db.apply_duel(duel_id, first.dice.value, second.dice.value)
    except Exception:
        logger.exception("Не удалось доиграть дуэль %s", duel_id)
        await db.release_duel(duel_id)
        await query.message.answer("Кубики не бросились. Вызов снова можно принять.")
        return
    if outcome is None:
        await query.message.answer("Эта дуэль уже посчитана.")
        return
    await query.message.answer(duel_result(outcome))


@router.message(Command("set"))
async def cmd_set(message: Message, command: CommandObject, db: Database, bot: Bot) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not _is_group(message):
        await message.answer(GROUP_ONLY)
        return
    if not await _is_admin(message, bot):
        await message.reply(ADMIN_ONLY)
        return
    size = parse_size(command.args or "")
    target = _target_user(message)
    if size is None or target is None:
        await message.reply("Ответь этой командой на сообщение игрока и укажи размер, например /set 120.")
        return
    await db.set_size(
        user_id=target.id,
        chat_id=message.chat.id,
        username=target.username,
        first_name=target.first_name or "Игрок",
        chat_title=message.chat.title,
        size=size,
    )
    name = escape_name(target.first_name, target.username, target.id)
    await message.reply(
        f"Текущий размер {name}: <b>{format_cm(size)}</b>.\n"
        "Следующий /dick пойдёт уже от этого числа."
    )


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
    except TelegramBadRequest:
        logger.info("Не смог поздороваться в чате %s", event.chat.id)
