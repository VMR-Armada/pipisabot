from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

from pipisa.battlecup import watch_battlecups
from pipisa.cleanup import TrackBotMessages, watch_cleanup
from pipisa.config import Config
from pipisa.db import Database
from pipisa.handlers import router

GROUP_COMMANDS = [
    BotCommand(command="dick", description="Сделать замер"),
    BotCommand(command="top_dick", description="Топ 10 этого чата"),
    BotCommand(command="top10_pics", description="Топ 10 картинкой"),
    BotCommand(command="global_top", description="Глобальный топ 10"),
    BotCommand(command="global_top_pic", description="Глобальный топ картинкой"),
    BotCommand(command="stats", description="Статистика картинкой"),
    BotCommand(command="duel", description="Вызвать ответом или по нику"),
    BotCommand(command="ffa", description="Открытый вызов, принять может любой"),
    BotCommand(command="battlecup", description="5 бросков, старт через 10 минут"),
    BotCommand(command="clear", description="Удалять сообщения бота через N минут"),
    BotCommand(command="help", description="Как играть"),
]

PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Как играть"),
]


async def _setup_bot(bot: Bot) -> None:
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_short_description("Ежедневная линейка для чатов")
    await bot.set_my_description(
        "Шуточная игра для групп. Раз в календарный день случайный замер от −5 до +10 см, "
        "топы чата и общий топ."
    )


async def run(config: Config) -> None:
    db = Database(
        config.db_path,
        grow_min=config.grow_min,
        grow_max=config.grow_max,
        zone=config.zone,
    )
    await db.connect()
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(db=db)
    dp.include_router(router)
    bot.session.middleware(TrackBotMessages(db))
    watch_cups = asyncio.create_task(watch_battlecups(bot, db))
    watch_messages = asyncio.create_task(watch_cleanup(bot, db))
    try:
        await _setup_bot(bot)
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,
        )
    finally:
        watch_cups.cancel()
        watch_messages.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch_cups
        with contextlib.suppress(asyncio.CancelledError):
            await watch_messages
        await db.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    try:
        asyncio.run(run(config))
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram не принял токен. Проверь BOT_TOKEN у @BotFather.") from None


if __name__ == "__main__":
    main()
