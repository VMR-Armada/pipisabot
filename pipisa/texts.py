from __future__ import annotations

import html
import random
from datetime import datetime, timezone, tzinfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipisa.db import BattlecupOutcome, DayReport, DuelOutcome, PeriodReview

HELP_TEXT = (
    "Я линейка — шуточная игра для групповых чатов.\n\n"
    "В каждом чате у игрока своя длина. Один раз за календарный день /dick "
    "меняет её на случайное целое число от −5 до +10 см. "
    "На следующий день замер снова доступен, даже если 24 часа ещё не прошли.\n\n"
    "/dick — сделать замер\n"
    "/top_dick — топ 10 этого чата\n"
    "/top10_pics — тот же топ картинкой: слева самый большой\n"
    "/global_top — топ 10 по всем чатам. Размер из этого чата, если человек здесь играет\n"
    "/global_top_pic — тот же глобальный топ картинкой\n"
    "/stats — карточка статистики\n"
    "/day — сухие итоги дня: замеры, счёт дуэлей и баттлкапы\n"
    "/week — то же за неделю, с понедельника\n"
    "/day_review — шутливые итоги дня: каждый замер этого чата, плюс дуэли и баттлкапы\n"
    "/week_review — шутливые итоги недели в этом чате\n"
    "/duel — дуэль на кубиках: ответом на сообщение или через /duel @ник. Вызов можно отменить, пока его не приняли\n"
    "/ffa — открытый вызов: принять может любой, кто уже делал замер\n"
    "/battlecup — турнир через 10 минут, можно указать свои: /battlecup 5. Пять бросков по очереди, между раундами минута, банк забирает большая сумма\n"
    "/clear 20 — удалять сообщения бота старше 20 минут. Непринятые дуэли и объявление баттлкапа остаются, пока они не закрыты\n"
    "/help — эта справка"
)

PRIVATE_WELCOME = (
    "Привет. Я играю в группах: добавь меня в чат и напиши там /dick.\n\n" + HELP_TEXT
)

GROUP_ONLY = "Эта команда работает в группе. Добавь меня в чат и напиши её там."


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


def duel_refusal(name: str, *, challenger: bool) -> str:
    if challenger:
        pool = (
            "Вызвал и сразу передумал. Впечатляет.",
            "Пока соперник читал, храбрость уже кончилась.",
            "Самый короткий вызов в этом чате.",
            "Сначала громко, потом тихонечко «Отказаться».",
            "Кубики остались без работы. Обидно за них.",
            "Вызов продержался меньше, чем анимация кубика.",
        )
    else:
        pool = (
            "Кубик даже не понадобился.",
            "Храбрость дошла до кнопки и развернулась.",
            "Можно было хотя бы притвориться, что не страшно.",
            "Линейка это запомнит. Она злопамятная.",
            "Самый безопасный бросок — тот, которого не было.",
            "Смелости хватило ровно на отказ.",
        )
    return f"{name} отказался от дуэли.\n{random.choice(pool)}"


def battlecup_announce(*, starts_in: int, stake: int, names: list[str]) -> str:
    pool = stake * len(names)
    lines = [
        "🏆 <b>Баттлкап</b>",
        f"Старт через {format_wait(starts_in)}.",
        f"Взнос <b>{format_cm(stake)}</b>. Сейчас в банке <b>{format_cm(pool)}</b>.",
        "Каждый делает 5 бросков по очереди, следующий раунд через минуту. У кого сумма выше, тот забирает банк. "
        "Если первых мест несколько, банк делится поровну.",
    ]
    if names:
        lines.append("")
        lines.append("Уже в игре:")
        lines.extend(f"• {name}" for name in names)
    else:
        lines.append("Пока никто не поставил. Нужны двое.")
    return "\n".join(lines)


# Номер анимации Telegram. 1 и 2 — мимо, оба без кеглей.
_PINS = {1: 0, 2: 0, 3: 3, 4: 4, 5: 5, 6: 6}


def pins_knocked(value: int) -> int:
    try:
        return _PINS[value]
    except KeyError:
        raise ValueError("В боулинге может выпасть только число от 1 до 6") from None


def bowling_score(pins: int) -> str:
    if pins >= 6:
        return "страйк, 6 кеглей"
    if pins <= 0:
        return "мимо, 0 кеглей"
    return f"{pins} {plural(pins, 'кегля', 'кегли', 'кеглей')}"


def day_text(report: DayReport) -> str:
    lines = ["Замеры:"]
    if report.measures:
        for measure in report.measures:
            lines.append(f"{_safe(measure.name)} {_measure_change(measure.delta)}")
    else:
        lines.append("нет")
    lines.append("Дуэли:")
    if report.duels:
        for duel in report.duels:
            lines.append(
                f"{_safe(duel.left)} {duel.left_wins}-{duel.right_wins}-{duel.ties} "
                f"{_safe(duel.right)} ({_signed_tight(duel.left_net)})"
            )
    else:
        lines.append("нет")
    lines.append("Баттлкапы:")
    if report.cups:
        for cup in report.cups:
            word = plural(cup.wins, "победа", "победы", "побед")
            lines.append(f"{_safe(cup.name)} {cup.wins} {word} (суммарный банк {cup.bank} см)")
    else:
        lines.append("нет")
    return "\n".join(lines)


def _measure_change(delta: int) -> str:
    if delta > 0:
        return f"+ {delta} см"
    if delta < 0:
        return f"- {abs(delta)} см"
    return "0 см"


def _signed_tight(value: int) -> str:
    if value > 0:
        return f"+{value} см"
    if value < 0:
        return f"-{abs(value)} см"
    return "0 см"


def review_text(review: PeriodReview, *, week: bool, zone: tzinfo | None = None) -> str:
    title = "Итоги недели" if week else "Итоги дня"
    when = "за неделю" if week else "сегодня"
    if review.plays == 0 and review.duels == 0 and review.cups == 0:
        return (
            f"📅 <b>{title}</b>\n\n"
            "Пока пусто. Ни замеров, ни дуэлей, ни баттлкапов. "
            "Линейка обиженно молчит. Разбудите её командой /dick."
        )
    lines = [f"📅 <b>{title}</b>", ""]
    if review.plays:
        lines.append(
            f"{when.capitalize()} линейка поработала <b>{review.plays}</b> "
            f"{plural(review.plays, 'раз', 'раза', 'раз')} и нарисовала суммарно "
            f"<b>{format_signed_cm(review.play_sum)}</b>."
        )
        if review.best_growth is not None:
            lines.append(
                f"{_safe(review.best_growth.name)} вырос сильнее всех: "
                f"<b>{format_signed_cm(review.best_growth.value)}</b>. Линейка явно была за него."
            )
        if review.worst_growth is not None:
            lines.append(
                f"{_safe(review.worst_growth.name)} усох на "
                f"<b>{format_cm(review.worst_growth.value)}</b>. "
                f"Бывает, линейка {'на этой неделе' if week else 'сегодня'} вредная."
            )
        if review.best_measure is not None:
            lines.append(
                f"Самый щедрый замер — {_safe(review.best_measure.name)}, "
                f"<b>{format_signed_cm(review.best_measure.value)}</b>."
            )
        if review.worst_measure is not None:
            lines.append(
                f"Самый обидный — {_safe(review.worst_measure.name)}, "
                f"<b>{format_cm(review.worst_measure.value)}</b>."
            )
        if not week:
            clock = zone or timezone.utc
            lines.append("")
            lines.append("Все замеры:")
            for measure in review.measures:
                stamp = datetime.fromtimestamp(measure.played_at, clock).strftime("%H:%M")
                lines.append(
                    f"{stamp} — {_safe(measure.name)}: <b>{format_signed_cm(measure.delta)}</b>, "
                    f"стало <b>{format_cm(measure.size_after)}</b>."
                )
        lines.append("")
    if review.duels:
        lines.append(
            f"Дуэлей: <b>{review.duels}</b>. "
            + (
                f"{_safe(review.duel_leader.name)} забрал <b>{review.duel_leader.value}</b> "
                f"{plural(review.duel_leader.value, 'победу', 'победы', 'побед')}. Кубик был на коротком поводке."
                if review.duel_leader is not None
                else "Все разошлись миром. Кубик сегодня дипломат."
            )
        )
        if review.duel_haul is not None:
            lines.append(
                f"Самая жирная дуэль: {_safe(review.duel_haul.winner)} против "
                f"{_safe(review.duel_haul.loser)}, <b>{format_cm(review.duel_haul.amount)}</b> ушли победителю."
            )
        lines.append("")
    if review.cups:
        lines.append(
            f"Баттлкапов: <b>{review.cups}</b>. "
            + (
                f"Первое место чаще у {_safe(review.cup_leader.name)} "
                f"(<b>{review.cup_leader.value}</b>)."
                if review.cup_leader is not None
                else "Первое место так и не выбрали. Банк обиделся."
            )
        )
        if review.cup_sponsor is not None:
            lines.append(
                f"{_safe(review.cup_sponsor.name)} оплатил чужой праздник: "
                f"<b>{format_cm(review.cup_sponsor.value)}</b>."
            )
        lines.append("")
    closing = (
        (
            "Неделя сдана. Линейка берёт выходной до понедельника. Шутка: /dick работает каждый день.",
            "Семь дней, одна линейка, ноль жалости.",
            "Неделя в архиве. Размеры остаются, стыд тоже.",
        )
        if week
        else (
            "День закрыт. Завтра линейка снова голодная.",
            "На сегодня хватит. Линейка идёт спать.",
            "Итог сдан. Кто не мерился, тот сам виноват.",
        )
    )
    lines.append(random.choice(closing))
    return "\n".join(lines).strip()


def split_html(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    chunk: list[str] = []
    size = 0
    for line in text.split("\n"):
        extra = len(line) if not chunk else len(line) + 1
        if chunk and size + extra > limit:
            parts.append("\n".join(chunk))
            chunk = [line]
            size = len(line)
        else:
            chunk.append(line)
            size += extra
    if chunk:
        parts.append("\n".join(chunk))
    return parts


def _safe(name: str) -> str:
    return html.escape(name.strip() or "Игрок")


def _winner_compliment(names: list[str]) -> str:
    if len(names) == 1:
        name = names[0]
        pool = (
            f"{name}, кегли сегодня были на твоей стороне.",
            f"{name} забирает банк так, будто иначе и быть не могло.",
            f"Красивый бросок, {name}.",
            f"{name}, дорожка сегодня работала на тебя.",
            f"Банк нашёл, кого хотел: {name}.",
        )
    else:
        listed = ", ".join(names)
        pool = (
            f"{listed} делят первое место. Дорожка не смогла выбрать одного.",
            f"Банк на нескольких руках: {listed}. Справедливо и немного обидно.",
            f"{listed}, бросок один на всех. Славу тоже делите.",
        )
    return random.choice(pool)


def _last_place_taunt(names: list[str]) -> str:
    if len(names) == 1:
        name = names[0]
        pool = (
            f"{name}, шар увидел кегли и сделал вид, что их нет.",
            f"Последнее место у {name}. Взнос ушёл на чужой праздник.",
            f"{name} сегодня спонсор победителя.",
            f"Кегли бросок {name} даже не заметили.",
            f"{name} закрывает таблицу. Снизу.",
        )
    else:
        listed = ", ".join(names)
        pool = (
            f"Последнее место делят {listed}. Редкая командная работа.",
            f"{listed} внизу таблицы. Шар сегодня был не в настроении.",
            f"{listed} оплатили чужое первое место. Щедро.",
        )
    return random.choice(pool)


def battlecup_round(
    *,
    throw_index: int,
    throw_count: int,
    rows: list[tuple[str, int, int]],
    following: bool = False,
) -> str:
    lines = [f"🎳 <b>Бросок {throw_index} из {throw_count}</b>"]
    for name, roll, total in rows:
        lines.append(f"{name}: <b>{bowling_score(roll)}</b>, всего {total}")
    if following:
        lines.append("Следующий бросок через минуту.")
    return "\n".join(lines)


def battlecup_result(outcome: BattlecupOutcome) -> str:
    lines = [f"🏆 Банк <b>{format_cm(outcome.pool)}</b>."]
    ordered = sorted(outcome.payouts, key=lambda payout: (-payout.roll, payout.user_id))
    for payout in ordered:
        name = escape_name(payout.first_name, payout.username, payout.user_id)
        lines.append(
            f"{name}: сумма <b>{payout.roll}</b>, сейчас <b>{format_cm(payout.size)}</b>"
        )
    winners = [payout for payout in outcome.payouts if payout.prize > 0]
    lines.append("")
    if len(winners) == 1:
        winner = winners[0]
        name = escape_name(winner.first_name, winner.username, winner.user_id)
        lines.append(f"{name} забирает весь банк, <b>{format_cm(winner.prize)}</b>.")
    else:
        prizes = {payout.prize for payout in winners}
        if len(prizes) == 1:
            lines.append(f"Первых мест {len(winners)}, каждому <b>{format_cm(winners[0].prize)}</b>.")
        else:
            lines.append("Первых мест несколько, банк разошёлся так:")
            for payout in winners:
                name = escape_name(payout.first_name, payout.username, payout.user_id)
                lines.append(f"{name}: <b>{format_cm(payout.prize)}</b>")
    winner_names = [escape_name(payout.first_name, payout.username, payout.user_id) for payout in winners]
    lines.append(_winner_compliment(winner_names))
    if outcome.payouts:
        worst = min(payout.roll for payout in outcome.payouts)
        last = [payout for payout in outcome.payouts if payout.roll == worst and payout.prize == 0]
        if last:
            last_names = [escape_name(payout.first_name, payout.username, payout.user_id) for payout in last]
            lines.append(_last_place_taunt(last_names))
    return "\n".join(lines)


def _older_than_minutes(minutes: int) -> str:
    number = abs(minutes) % 100
    word = "минуты" if number % 10 == 1 and number != 11 else "минут"
    return f"{minutes} {word}"


def clear_enabled(minutes: int) -> str:
    return (
        f"Буду удалять свои сообщения старше {_older_than_minutes(minutes)}.\n"
        "Непринятые дуэли оставлю, пока их не закроют. "
        "Объявление баттлкапа тоже оставлю, пока он не начался."
    )


def clear_disabled() -> str:
    return "Очистка выключена. Свои сообщения больше не удаляю."


def clear_status(minutes: int | None) -> str:
    if minutes is None:
        return "Очистка выключена. Включить: /clear 20"
    return (
        f"Сейчас удаляю свои сообщения старше {_older_than_minutes(minutes)}.\n"
        "Выключить: /clear 0"
    )


def ffa_challenge(challenger: str) -> str:
    return (
        f"📏 {challenger} бросает открытый вызов.\n"
        "Принять может любой, кто уже делал замер.\n"
        "Пока вызов не принят, от него можно отказаться."
    )


def duel_challenge(challenger: str, opponent: str) -> str:
    return (
        f"📏 {challenger} вызывает {opponent} на дуэль.\n"
        "Каждый бросает кубик от 1 до 6. У кого число больше, "
        "тот забирает у соперника разницу.\n"
        "Пока вызов не принят, от него можно отказаться."
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
    duel_wins: int,
    duel_losses: int,
    duel_ties: int,
    cup_wins: int,
    cup_played: int,
    rivals: list[tuple[str, int, int, int, int]],
    next_free_text: str,
) -> str:
    safe = html.escape(name)
    best = format_signed_cm(best_gain) if best_gain else "—"
    worst = format_signed_cm(worst_loss) if worst_loss else "—"
    duels = f"{duel_wins}–{duel_losses}" + (f"–{duel_ties}" if duel_ties else "")
    cups = f"{cup_wins} из {cup_played}" if cup_played else "—"
    lines = [
        f"📏 <b>{safe}</b>",
        f"Сейчас: <b>{format_cm(size)}</b>",
        f"Место в чате: {rank} из {total}",
        f"Замеров: {plays}",
        f"Лучший рост: {best}",
        f"Худший замер: {worst}",
        f"Дуэли: {duels}",
        f"Баттлкапы: {cups}",
    ]
    if rivals:
        lines.append("Счёт с соперниками:")
        for rival_name, wins, losses, ties, net in rivals[:8]:
            score = f"{wins}–{losses}" + (f"–{ties}" if ties else "")
            lines.append(f"• {html.escape(rival_name)} — {score} ({format_signed_cm(net)})")
        if len(rivals) > 8:
            lines.append(f"и ещё {len(rivals) - 8}")
    lines.append(next_free_text)
    return "\n".join(lines)
