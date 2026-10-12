from __future__ import annotations

import html
import random

HELP_TEXT = (
    "Я линейка — шуточная игра для групповых чатов.\n\n"
    "В каждом чате у игрока своя длина. Раз в 24 часа /dick "
    "меняет её на случайное целое число от −5 до +10 см.\n\n"
    "/dick — сделать замер\n"
    "/top_dick — топ 10 этого чата\n"
    "/global_top — топ 10 по всем чатам\n"
    "/stats — карточка статистики\n"
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
        lines.append(f"Следующий замер через {format_wait(wait_seconds)}.")
    else:
        lines.append("Следующий замер уже доступен.")
    return "\n".join(lines)


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
        f"📏 <b>{name}</b>, замер уже был.\n"
        f"Сейчас: <b>{format_cm(size)}</b>.\n"
        f"Следующий через {format_wait(wait_seconds)}."
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
