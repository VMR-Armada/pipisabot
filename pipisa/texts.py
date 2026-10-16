from __future__ import annotations

import html
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipisa.db import DuelOutcome

HELP_TEXT = (
    "Я линейка — шуточная игра для групповых чатов.\n\n"
    "В каждом чате у игрока своя длина. Один раз за календарный день /dick "
    "меняет её на случайное целое число от −5 до +10 см. "
    "На следующий день замер снова доступен, даже если 24 часа ещё не прошли.\n\n"
    "/dick — сделать замер\n"
    "/top_dick — топ 10 этого чата\n"
    "/global_top — топ 10 по всем чатам\n"
    "/stats — карточка статистики\n"
    "/duel — дуэль на кубиках: ответом на сообщение или через /duel @ник. Вызов можно отменить, пока его не приняли\n"
    "/help — эта справка\n\n"
    "Админ чата может перенести уже известные размеры:\n"
    "/import — списком, каждая строка как @ник 120\n"
    "/set 120 — ответом на сообщение игрока"
)

PRIVATE_WELCOME = (
    "Привет. Я играю в группах: добавь меня в чат и напиши там /dick.\n\n" + HELP_TEXT
)

GROUP_ONLY = "Эта команда работает в группе. Добавь меня в чат и напиши её там."

ADMIN_ONLY = "Переносить размеры может только админ этого чата."

IMPORT_USAGE = (
    "Пришли список в этом сообщении или ответь командой /import на сообщение со списком.\n\n"
    "@ivan 120\n"
    "1. Маша (@masha) — 80 см\n"
    "Петя -5\n\n"
    "Размер встанет как текущий. Следующий /dick прибавит к нему новый замер.\n"
    "Строка с @ником надёжнее: бот узнает человека, даже если имя в Telegram другое."
)


def plural(value: int, one: str, few: str, many: str) -> str:
    number = abs(value) % 100
    if 11 <= number <= 14:
        return many
    tail = number % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def format_cm(value: int) -> str:
    if value < 0:
        return f"−{abs(value)} см"
    return f"{value} см"


def format_signed_cm(value: int) -> str:
    if value > 0:
        return f"+{value} см"
    return format_cm(value)


def format_wait(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "меньше минуты"
    minutes_total = seconds // 60
    days, hours = divmod(minutes_total // 60, 24)
    minutes = minutes_total % 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} {plural(days, 'день', 'дня', 'дней')}")
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if minutes and not days:
        parts.append(f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}")
    return " ".join(parts) or "меньше минуты"


def display_name(first_name: str | None, username: str | None, user_id: int) -> str:
    name = (first_name or "").strip() or "Игрок"
    if username:
        return f"{name} (@{username})"
    return name


def escape_name(first_name: str | None, username: str | None, user_id: int) -> str:
    return html.escape(display_name(first_name, username, user_id))


def _flavor(delta: int) -> str:
    if delta >= 7:
        pool = (
            "Сегодня линейка щедрая.",
            "Редкий удачный замер.",
            "Так можно и в топ забраться.",
        )
    elif delta > 0:
        pool = (
            "Небольшой, но честный плюс.",
            "Рост есть.",
            "Завтра можно лучше, но и это засчитано.",
        )
    elif delta == 0:
        pool = (
            "Стабильность — тоже результат.",
            "Линейка пожала плечами.",
            "Ни туда ни сюда.",
        )
    elif delta <= -4:
        pool = (
            "Сегодня линейка суровая.",
            "Бывает и так.",
            "Завтрашний замер ещё впереди.",
        )
    else:
        pool = (
            "Лёгкий минус, не катастрофа.",
            "Чуть ужалось.",
            "Следующий замер может всё вернуть.",
        )
    return random.choice(pool)


def grow_message(
    *,
    first_name: str | None,
    username: str | None,
    user_id: int,
    delta: int,
    size: int,
    wait_seconds: int,
) -> str:
    name = escape_name(first_name, username, user_id)
    if delta > 0:
        change = f"выросла на <b>{delta} см</b>"
    elif delta < 0:
        change = f"уменьшилась на <b>{abs(delta)} см</b>"
    else:
        change = "<b>не изменилась</b>"
    lines = [
        f"📏 <b>{name}</b>, пиписа {change}.",
        f"Сейчас: <b>{format_cm(size)}</b>.",
        _flavor(delta),
    ]
    if wait_seconds > 0:
        lines.append(f"Следующий замер — завтра, через {format_wait(wait_seconds)}.")
    else:
        lines.append("Следующий замер уже доступен.")
    return "\n".join(lines)


def duel_challenge(challenger: str, opponent: str) -> str:
    return (
        f"📏 {challenger} вызывает {opponent} на дуэль.\n"
        "Каждый бросает кубик от 1 до 6. У кого число больше, "
        "тот забирает у соперника разницу.\n"
        "Пока вызов не принят, его можно отменить."
    )


def duel_result(outcome: DuelOutcome) -> str:
    challenger = escape_name(outcome.challenger_name, outcome.challenger_username, 0)
    opponent = escape_name(outcome.opponent_name, outcome.opponent_username, 0)
    if outcome.diff == 0:
        return (
            f"Ничья: оба выбросили <b>{outcome.challenger_roll}</b>.\n"
            "Размеры не изменились."
        )
    if outcome.challenger_roll > outcome.opponent_roll:
        winner, winner_size = challenger, outcome.challenger_size
        loser, loser_size = opponent, outcome.opponent_size
    else:
        winner, winner_size = opponent, outcome.opponent_size
        loser, loser_size = challenger, outcome.challenger_size
    return (
        f"{challenger} выбросил <b>{outcome.challenger_roll}</b>, "
        f"{opponent} — <b>{outcome.opponent_roll}</b>.\n"
        f"{winner} забирает <b>{outcome.diff} см</b>.\n"
        f"{winner}: <b>{format_cm(winner_size)}</b>.\n"
        f"{loser}: <b>{format_cm(loser_size)}</b>."
    )


def cooldown_message(
    *,
    first_name: str | None,
    username: str | None,
    user_id: int,
    size: int,
    wait_seconds: int,
) -> str:
    name = escape_name(first_name, username, user_id)
    return (
        f"📏 <b>{name}</b>, замер на сегодня уже был.\n"
        f"Сейчас: <b>{format_cm(size)}</b>.\n"
        f"Завтра можно снова, через {format_wait(wait_seconds)}."
    )


def top_message(
    title: str,
    rows: list[tuple[int, str, int, str | None]],
    standing: tuple[int, int, int] | None,
) -> str:
    """rows: (user_id, escaped name, size, extra note). standing: rank, total, size."""
    if not rows:
        return f"<b>{title}</b>\n\nПока пусто. Первый замер — команда /dick."
    lines = [f"<b>{title}</b>", ""]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for index, (user_id, name, size, note) in enumerate(rows, start=1):
        mark = medals.get(index, f"{index}.")
        extra = f" · {html.escape(note)}" if note else ""
        lines.append(f"{mark} {name} — <b>{format_cm(size)}</b>{extra}")
    if standing is not None:
        rank, total, size = standing
        lines.append("")
        lines.append(
            f"Ты: {rank} {plural(rank, 'место', 'место', 'место')} из {total}, "
            f"<b>{format_cm(size)}</b>."
        )
    return "\n".join(lines)


def stats_fallback(
    *,
    name: str,
    size: int,
    rank: int,
    total: int,
    plays: int,
    best_gain: int,
    worst_loss: int,
    next_free_text: str,
) -> str:
    safe = html.escape(name)
    best = format_signed_cm(best_gain) if best_gain else "—"
    worst = format_signed_cm(worst_loss) if worst_loss else "—"
    return (
        f"📏 <b>{safe}</b>\n"
        f"Сейчас: <b>{format_cm(size)}</b>\n"
        f"Место в чате: {rank} из {total}\n"
        f"Замеров: {plays}\n"
        f"Лучший рост: {best}\n"
        f"Худший замер: {worst}\n"
        f"{next_free_text}"
    )
