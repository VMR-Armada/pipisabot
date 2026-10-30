from __future__ import annotations

from io import BytesIO
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from pipisa.battlecup import countdown_seconds, parse_battlecup_minutes, play_battlecup, round_pause
from pipisa.day import next_midnight, period_start

from pipisa.db import (
    Battlecup,
    BattlecupOutcome,
    BattlecupPayout,
    BattlecupPlayer,
    Database,
    DayReport,
    DuelHaul,
    DuelScore,
    CupStanding,
    Entry,
    Measure,
    NamedValue,
    PeriodReview,
    _day_duels,
    play_streaks,
    prize_shares,
)
from pipisa.handlers import _rows, on_telegram_error
from pipisa.stats_card import CardData, _font, render_stats
from pipisa.telegram_errors import delivery_target, describe_telegram_error, notice_with_cause, tell_user
from pipisa.top_card import render_top
from pipisa.texts import (
    battlecup_result,
    battlecup_round,
    bowling_score,
    dart_points,
    dart_score,
    day_text,
    pins_knocked,
    review_text,
    split_html,
    clear_disabled,
    clear_enabled,
    duel_refusal,
    format_wait,
    plural,
    top_message,
)
from pipisa.transfer import ImportItem, parse_import_text, parse_size


class TextTests(unittest.TestCase):
    def test_plural(self) -> None:
        self.assertEqual(plural(1, "попытка", "попытки", "попыток"), "попытка")
        self.assertEqual(plural(2, "попытка", "попытки", "попыток"), "попытки")
        self.assertEqual(plural(5, "попытка", "попытки", "попыток"), "попыток")
        self.assertEqual(plural(11, "попытка", "попытки", "попыток"), "попыток")
        self.assertEqual(plural(21, "час", "часа", "часов"), "час")

    def test_global_top_omits_profile_mention(self) -> None:
        entry = Entry(
            user_id=1,
            chat_id=-100,
            size=10,
            username="anna",
            first_name="Анна",
            chat_title="Чат",
        )
        text = top_message(
            "Глобальный топ 10",
            _rows([entry], with_chat=True, show_username=False),
            None,
        )
        self.assertIn("Анна", text)
        self.assertIn("Чат", text)
        self.assertNotIn("@anna", text)

        chat_text = top_message("Топ 10 этого чата", _rows([entry], with_chat=False), None)
        self.assertIn("@anna", chat_text)

    def test_duel_refusal_taunts(self) -> None:
        with patch("pipisa.texts.random.choice", side_effect=lambda pool: pool[0]):
            backed_out = duel_refusal("Анна", challenger=True)
            declined = duel_refusal("Борис", challenger=False)
        self.assertEqual(backed_out, "Анна отказался от дуэли.\nВызвал и сразу передумал. Впечатляет.")
        self.assertEqual(declined, "Борис отказался от дуэли.\nКубик даже не понадобился.")

    def test_prize_shares_keep_the_whole_pool(self) -> None:
        shares = prize_shares(10, [3, 1, 2])
        self.assertEqual(shares, {1: 4, 2: 3, 3: 3})
        self.assertEqual(sum(shares.values()), 10)

    def test_battlecup_result_flatters_the_winner_and_last_place(self) -> None:
        outcome = BattlecupOutcome(
            pool=6,
            payouts=[
                BattlecupPayout(1, "Анна", None, 6, 6, 24),
                BattlecupPayout(2, "Борис", None, 3, 0, 18),
                BattlecupPayout(3, "Вова", None, 1, 0, 16),
            ],
        )
        with patch("pipisa.texts.random.choice", side_effect=lambda pool: pool[0]):
            text = battlecup_result(outcome)
        self.assertIn("Анна: сумма <b>6</b>", text)
        self.assertIn("Борис: сумма <b>3</b>", text)
        self.assertIn("Вова: сумма <b>1</b>", text)
        self.assertIn("Анна, кегли сегодня были на твоей стороне.", text)
        self.assertIn("Вова, шар увидел кегли и сделал вид, что их нет.", text)
        self.assertNotIn("Борис, шар", text)

    def test_bowling_score_names_the_pins(self) -> None:
        self.assertEqual(pins_knocked(1), 0)
        self.assertEqual(pins_knocked(2), 1)
        self.assertEqual(pins_knocked(3), 3)
        self.assertEqual(pins_knocked(4), 4)
        self.assertEqual(pins_knocked(5), 5)
        self.assertEqual(pins_knocked(6), 6)
        self.assertEqual(bowling_score(0), "мимо, 0 кеглей")
        self.assertEqual(bowling_score(1), "1 кегля")
        self.assertEqual(bowling_score(2), "2 кегли")
        self.assertEqual(bowling_score(4), "4 кегли")
        self.assertEqual(bowling_score(5), "5 кеглей")
        self.assertEqual(bowling_score(6), "страйк, 6 кеглей")

    def test_dart_scores_rings_and_a_bullseye(self) -> None:
        self.assertEqual([dart_points(value) for value in range(1, 7)], [0, 1, 2, 3, 4, 5])
        self.assertEqual(dart_score(0), "мимо, 0 очков")
        self.assertEqual(dart_score(1), "внешнее кольцо, 1 очко")
        self.assertEqual(dart_score(2), "второе кольцо, 2 очка")
        self.assertEqual(dart_score(4), "внутреннее кольцо, 4 очка")
        self.assertEqual(dart_score(5), "яблочко, 5 очков")

    def test_period_starts_at_local_midnight_or_monday(self) -> None:
        friday = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
        moment = int(friday.timestamp())
        self.assertEqual(period_start(moment, timezone.utc, week=False), int(datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(period_start(moment, timezone.utc, week=True), int(datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp()))

    def test_review_tells_the_day_and_stays_quiet_when_empty(self) -> None:
        empty = PeriodReview(
            plays=0,
            play_sum=0,
            best_growth=None,
            worst_growth=None,
            best_measure=None,
            worst_measure=None,
            duels=0,
            duel_leader=None,
            duel_haul=None,
            cups=0,
            cup_leader=None,
            cup_sponsor=None,
        )
        quiet = review_text(empty, week=False)
        self.assertIn("Итоги дня", quiet)
        self.assertIn("Пока пусто", quiet)
        self.assertIn("/dick", quiet)

        busy = PeriodReview(
            plays=2,
            play_sum=8,
            best_growth=NamedValue("Анна", 10),
            worst_growth=NamedValue("Борис", -2),
            best_measure=NamedValue("Анна", 10),
            worst_measure=NamedValue("Борис", -2),
            duels=1,
            duel_leader=NamedValue("Анна", 1),
            duel_haul=DuelHaul("Анна", "Борис", 4),
            cups=1,
            cup_leader=NamedValue("Анна", 1),
            cup_sponsor=NamedValue("Борис", -2),
        )
        with patch("pipisa.texts.random.choice", side_effect=lambda pool: pool[0]):
            text = review_text(busy, week=True)
        self.assertIn("Итоги недели", text)
        self.assertIn("За неделю линейка поработала <b>2</b> раза", text)
        self.assertIn("Анна вырос сильнее всех: <b>+10 см</b>", text)
        self.assertIn("Борис усох на <b>−2 см</b>", text)
        self.assertIn("Анна против Борис, <b>4 см</b> ушли победителю", text)
        self.assertIn("оплатил чужой праздник", text)
        self.assertIn("Неделя сдана", text)
        self.assertNotIn("Все замеры", text)

        morning = int(datetime(2026, 10, 30, 9, tzinfo=timezone.utc).timestamp())
        evening = int(datetime(2026, 10, 30, 18, 5, tzinfo=timezone.utc).timestamp())
        listed = PeriodReview(
            plays=2,
            play_sum=8,
            best_growth=NamedValue("Анна", 10),
            worst_growth=NamedValue("Борис", -2),
            best_measure=NamedValue("Анна", 10),
            worst_measure=NamedValue("Борис", -2),
            duels=0,
            duel_leader=None,
            duel_haul=None,
            cups=0,
            cup_leader=None,
            cup_sponsor=None,
            measures=(
                Measure("Борис", -2, 13, morning),
                Measure("Анна", 10, 30, evening),
            ),
        )
        with patch("pipisa.texts.random.choice", side_effect=lambda pool: pool[0]):
            day = review_text(listed, week=False, zone=timezone.utc)
        roster = day.split("Все замеры:", 1)[1]
        self.assertIn("09:00 — Борис: <b>−2 см</b>, стало <b>13 см</b>.", roster)
        self.assertIn("18:05 — Анна: <b>+10 см</b>, стало <b>30 см</b>.", roster)
        self.assertLess(roster.index("09:00"), roster.index("18:05"))

    def test_long_review_is_split_on_line_breaks(self) -> None:
        text = "\n".join(f"строка {index}" for index in range(10))
        parts = split_html(text, limit=20)
        self.assertGreater(len(parts), 1)
        self.assertEqual("\n".join(parts), text)
        self.assertTrue(all(len(part) <= 20 or "\n" not in part for part in parts))

    def test_day_report_is_a_plain_list(self) -> None:
        text = day_text(
            DayReport(
                measures=(Measure("Анна", 10, 24, 1), Measure("Борис", -3, 12, 2)),
                duels=(DuelScore("Анна", "Борис", 2, 1, 0, 4),),
                cups=(CupStanding("Анна", 2, 14), CupStanding("Борис", 1, 4)),
            )
        )
        self.assertEqual(
            text,
            "\n".join(
                [
                    "Замеры:",
                    "Анна + 10 см",
                    "Борис - 3 см",
                    "Дуэли:",
                    "Анна 2-1-0 Борис (+4 см)",
                    "Баттлкапы:",
                    "Анна 2 победы (суммарный банк 14 см)",
                    "Борис 1 победа (суммарный банк 4 см)",
                ]
            ),
        )
        empty = day_text(DayReport((), (), ()))
        self.assertIn("Замеры:\nнет", empty)
        self.assertIn("Дуэли:\nнет", empty)
        self.assertIn("Баттлкапы:\nнет", empty)

    def test_day_duels_keep_a_personal_score(self) -> None:
        scores = _day_duels(
            [
                (1, 1, "Анна", -5, 2, 100),
                (1, 2, "Борис", 5, 1, 100),
                (2, 2, "Борис", 0, 0, 200),
                (2, 1, "Анна", 0, 0, 200),
            ]
        )
        self.assertEqual(scores, (DuelScore("Борис", "Анна", 1, 0, 1, 5),))

    def test_clear_names_the_age(self) -> None:
        self.assertIn("старше 20 минут", clear_enabled(20))
        self.assertIn("старше 1 минуты", clear_enabled(1))
        self.assertIn("старше 21 минуты", clear_enabled(21))
        self.assertIn("больше не удаляю", clear_disabled())

    def test_play_streaks_sum_until_zero_or_the_opposite_sign(self) -> None:
        self.assertEqual(play_streaks([5, 3, 7, 0]), (15, 0))
        self.assertEqual(play_streaks([-2, -4, -1, 5]), (5, -7))
        self.assertEqual(play_streaks([2, 2, -1, 9, 0, -3]), (9, -3))

    def test_battlecup_countdown_steps_once_a_minute(self) -> None:
        start = 1_000
        self.assertEqual(countdown_seconds(start + 300, start), 300)
        self.assertEqual(countdown_seconds(start + 300, start + 59), 300)
        self.assertEqual(countdown_seconds(start + 300, start + 60), 240)
        self.assertEqual(countdown_seconds(start + 300, start + 299), 60)
        self.assertEqual(countdown_seconds(start + 300, start + 300), 0)

    def test_battlecup_without_minutes_starts_in_ten(self) -> None:
        self.assertEqual(parse_battlecup_minutes(None), 10)
        self.assertEqual(parse_battlecup_minutes("  "), 10)
        self.assertEqual(parse_battlecup_minutes("5"), 5)
        self.assertIsNone(parse_battlecup_minutes("0"))
        self.assertIsNone(parse_battlecup_minutes("скоро"))

    def test_battlecup_round_lists_the_throw_and_total(self) -> None:
        text = battlecup_round(
            throw_index=1,
            throw_count=5,
            rows=[("Анна", 6, 6), ("Борис", 3, 3)],
        )
        self.assertIn("Бросок 1 из 5", text)
        self.assertIn("Анна: <b>страйк, 6 кеглей</b>, всего 6", text)
        self.assertIn("Борис: <b>3 кегли</b>, всего 3", text)
        self.assertNotIn("Следующий бросок", text)
        following = battlecup_round(
            throw_index=1,
            throw_count=5,
            rows=[("Анна", 6, 6)],
            following=True,
        )
        self.assertIn("Следующий бросок через минуту.", following)

    def test_battlecup_rounds_start_a_minute_apart(self) -> None:
        self.assertEqual(round_pause(0), 60)
        self.assertEqual(round_pause(6), 54)
        self.assertEqual(round_pause(60), 0)
        self.assertEqual(round_pause(75), 0)

    def test_wait(self) -> None:
        self.assertEqual(format_wait(30), "меньше минуты")
        self.assertEqual(format_wait(60), "1 минуту")
        self.assertEqual(format_wait(2 * 3600 + 5 * 60), "2 часа 5 минут")
        self.assertEqual(format_wait(26 * 3600), "1 день 2 часа")

    def test_parse_import(self) -> None:
        items, broken = parse_import_text(
            "Топ чата\n"
            "1. Анна (@anna) — 27 см\n"
            "@boris 10\n"
            "Петя −5\n"
            "просто текст\n"
            "кривая 12х\n"
        )
        self.assertEqual(
            [(item.first_name, item.username, item.size) for item in items],
            [("Анна", "anna", 27), ("boris", "boris", 10), ("Петя", None, -5)],
        )
        self.assertEqual(broken, ["кривая 12х"])
        self.assertEqual(parse_size("120 см"), 120)
        self.assertEqual(parse_size("−3"), -3)
        self.assertIsNone(parse_size("много"))


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.now = 1_700_000_000
        self.zone = timezone.utc
        self.db = Database(
            Path(self._dir.name) / "test.db",
            grow_min=-5,
            grow_max=10,
            zone=self.zone,
            clock=lambda: self.now,
        )
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._dir.cleanup()

    async def test_cooldown_and_tops(self) -> None:
        first = await self.db.try_grow(
            user_id=1,
            chat_id=-100,
            username="ann",
            first_name="Анна",
            chat_title="Чат",
            rng=lambda: 4,
        )
        self.assertTrue(first.ok)
        self.assertEqual(first.delta, 4)
        self.assertEqual(first.size, 4)

        played_at = self.now
        boundary = next_midnight(played_at, self.zone)
        self.now += 40
        blocked = await self.db.try_grow(
            user_id=1,
            chat_id=-100,
            username="ann",
            first_name="Анна",
            chat_title="Чат",
            rng=lambda: 9,
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.wait_seconds, boundary - self.now)
        self.assertEqual(blocked.size, 4)
        self.assertLess(blocked.wait_seconds, 24 * 3600)

        await self.db.try_grow(
            user_id=2,
            chat_id=-100,
            username=None,
            first_name="Борис",
            chat_title="Чат",
            rng=lambda: 10,
        )
        await self.db.try_grow(
            user_id=1,
            chat_id=-200,
            username="ann",
            first_name="Анна",
            chat_title="Другой",
            rng=lambda: 8,
        )

        entries, standing = await self.db.chat_top(-100, 1)
        self.assertEqual([entry.user_id for entry in entries], [2, 1])
        self.assertIsNotNone(standing)
        assert standing is not None
        self.assertEqual(standing.rank, 2)
        self.assertEqual(standing.total, 2)
        self.assertEqual(standing.size, 4)

        global_entries, global_standing = await self.db.global_top(1, chat_id=-100)
        self.assertEqual([entry.user_id for entry in global_entries], [2, 1])
        self.assertEqual(global_entries[1].size, 4)
        self.assertEqual(global_entries[1].chat_title, "Чат")
        assert global_standing is not None
        self.assertEqual(global_standing.size, 4)
        self.assertEqual(global_standing.rank, 2)

        best_entries, best_standing = await self.db.global_top(1)
        self.assertEqual(best_entries[1].size, 8)
        self.assertEqual(best_entries[1].chat_title, "Другой")
        assert best_standing is not None
        self.assertEqual(best_standing.size, 8)

        self.now = boundary
        free_again = await self.db.try_grow(
            user_id=1,
            chat_id=-100,
            username="ann",
            first_name="Анна",
            chat_title="Чат",
            rng=lambda: 0,
        )
        self.assertTrue(free_again.ok)
        self.assertEqual(free_again.size, 4)

        stats = await self.db.get_stats(1, -100)
        assert stats is not None
        self.assertEqual(stats.deltas, [4, 0])
        self.assertEqual(stats.best_gain, 4)
        self.assertEqual(stats.worst_loss, 0)
        self.assertEqual(stats.plays, 2)

    async def test_stats_use_growth_streaks(self) -> None:
        rolls = [5, 3, 7, 0, -2, -4, 1]
        for delta in rolls:
            grown = await self.db.try_grow(
                user_id=1,
                chat_id=-100,
                username="ann",
                first_name="Анна",
                chat_title="Чат",
                rng=lambda value=delta: value,
            )
            self.assertTrue(grown.ok)
            self.now = next_midnight(self.now, self.zone)
        stats = await self.db.get_stats(1, -100)
        assert stats is not None
        self.assertEqual(stats.best_gain, 15)
        self.assertEqual(stats.worst_loss, -6)
        self.assertEqual(stats.deltas, rolls)

    async def test_import_keeps_size_until_next_grow(self) -> None:
        applied, pending = await self.db.import_sizes(
            chat_id=-100,
            chat_title="Чат",
            items=[
                ImportItem(first_name="Анна", username="ann", size=27),
                ImportItem(first_name="Петя", username=None, size=-5),
            ],
        )
        self.assertEqual(applied, [])
        self.assertEqual(len(pending), 2)

        entries, standing = await self.db.chat_top(-100, 1, username="ann", first_name="Анна")
        self.assertEqual([entry.size for entry in entries], [27, -5])
        assert standing is not None
        self.assertEqual(standing.size, 27)
        self.assertEqual(standing.rank, 1)

        claimed = await self.db.claim_import(
            user_id=1,
            chat_id=-100,
            username="ann",
            first_name="Анна",
            chat_title="Чат",
        )
        self.assertTrue(claimed)
        grown = await self.db.try_grow(
            user_id=1,
            chat_id=-100,
            username="ann",
            first_name="Анна",
            chat_title="Чат",
            rng=lambda: 4,
        )
        self.assertTrue(grown.ok)
        self.assertEqual(grown.size, 31)

        entries, _standing = await self.db.chat_top(-100, 1)
        self.assertEqual([entry.size for entry in entries], [31, -5])

    async def test_duel_takes_the_difference(self) -> None:
        await self.db.set_size(
            user_id=1,
            chat_id=-100,
            username="anna",
            first_name="Анна",
            chat_title="Чат",
            size=20,
        )
        await self.db.set_size(
            user_id=2,
            chat_id=-100,
            username="boris",
            first_name="Борис",
            chat_title="Чат",
            size=15,
        )
        found = await self.db.find_player_by_username(-100, "Boris")
        assert found is not None
        self.assertEqual(found.user_id, 2)

        duel_id = await self.db.create_duel(chat_id=-100, challenger_id=1, opponent_id=2)
        assert duel_id is not None
        self.assertIsNone(await self.db.create_duel(chat_id=-100, challenger_id=1, opponent_id=2))
        self.assertFalse(await self.db.claim_duel(duel_id, 1))
        self.assertTrue(await self.db.claim_duel(duel_id, 2))

        outcome = await self.db.apply_duel(duel_id, 6, 2)
        assert outcome is not None
        self.assertEqual(outcome.diff, 4)
        self.assertEqual(outcome.challenger_size, 24)
        self.assertEqual(outcome.opponent_size, 11)
        winner = await self.db.get_stats(1, -100)
        loser = await self.db.get_stats(2, -100)
        assert winner is not None and loser is not None
        self.assertEqual(winner.duel_wins, 1)
        self.assertEqual(winner.contests, [("duel", 4)])
        self.assertEqual(winner.rivals, [("Борис", 1, 0, 0, 4)])
        self.assertEqual(loser.duel_losses, 1)
        self.assertEqual(loser.contests, [("duel", -4)])
        self.assertEqual(loser.rivals, [("Анна", 0, 1, 0, -4)])
        self.assertIsNone(await self.db.apply_duel(duel_id, 6, 2))

        tie_id = await self.db.create_duel(chat_id=-100, challenger_id=2, opponent_id=1)
        assert tie_id is not None
        self.assertTrue(await self.db.decline_duel(tie_id, 1))
        anna = await self.db.find_player(1, -100)
        boris = await self.db.find_player(2, -100)
        assert anna is not None and boris is not None
        self.assertEqual(anna.size, 24)
        self.assertEqual(boris.size, 11)

        cancel_id = await self.db.create_duel(chat_id=-100, challenger_id=1, opponent_id=2)
        assert cancel_id is not None
        open_duel = await self.db.find_open_duel(-100, 2, 1)
        assert open_duel is not None
        self.assertEqual(open_duel.id, cancel_id)
        self.assertFalse(await self.db.cancel_duel(cancel_id, 2))
        self.assertTrue(await self.db.cancel_duel(cancel_id, 1))
        self.assertFalse(await self.db.cancel_duel(cancel_id, 1))
        self.assertFalse(await self.db.claim_duel(cancel_id, 2))
        self.assertIsNone(await self.db.find_open_duel(-100, 1, 2))
        replacement = await self.db.create_duel(chat_id=-100, challenger_id=1, opponent_id=2)
        assert replacement is not None
        self.assertTrue(await self.db.claim_duel(replacement, 2))
        self.assertFalse(await self.db.cancel_duel(replacement, 1))
        anna = await self.db.find_player(1, -100)
        boris = await self.db.find_player(2, -100)
        assert anna is not None and boris is not None
        self.assertEqual(anna.size, 24)
        self.assertEqual(boris.size, 11)

    async def test_battlecup_pays_the_pool(self) -> None:
        for user_id, username, first_name, size in (
            (1, "anna", "Анна", 20),
            (2, "boris", "Борис", 20),
            (3, "vova", "Вова", 20),
        ):
            await self.db.set_size(
                user_id=user_id,
                chat_id=-100,
                username=username,
                first_name=first_name,
                chat_title="Чат",
                size=size,
            )

        future = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now + 300)
        assert future is not None
        self.assertEqual(await self.db.claim_due_battlecups(self.now), [])
        self.assertIsNone(await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now))
        darts = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now + 10_000, kind="darts")
        self.assertIsNotNone(darts)
        self.assertIsNone(await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now, kind="darts"))

        self.assertEqual(await self.db.join_battlecup(future, 1), "joined")
        self.assertEqual(await self.db.join_battlecup(future, 1), "already")
        self.assertEqual(await self.db.join_battlecup(future, 9), "unknown")
        anna = await self.db.find_player(1, -100)
        assert anna is not None
        self.assertEqual(anna.size, 18)

        self.assertEqual(await self.db.join_battlecup(future, 2), "joined")
        claimed = await self.db.claim_due_battlecups(self.now + 300)
        self.assertEqual([cup.id for cup in claimed], [future])
        self.assertEqual(await self.db.join_battlecup(future, 3), "closed")
        boris = await self.db.find_player(2, -100)
        assert boris is not None
        self.assertEqual(boris.size, 18)

        outcome = await self.db.finish_battlecup(future, {1: 6, 2: 2})
        assert outcome is not None
        self.assertEqual(outcome.pool, 4)
        self.assertIsNone(await self.db.finish_battlecup(future, {1: 6, 2: 2}))
        anna = await self.db.find_player(1, -100)
        boris = await self.db.find_player(2, -100)
        assert anna is not None and boris is not None
        self.assertEqual(anna.size, 22)
        self.assertEqual(boris.size, 18)
        winner = await self.db.get_stats(1, -100)
        loser = await self.db.get_stats(2, -100)
        assert winner is not None and loser is not None
        self.assertEqual(winner.cup_wins, 1)
        self.assertEqual(winner.cup_played, 1)
        self.assertEqual(winner.contests[-1], ("battlecup", 2))
        self.assertEqual(loser.cup_wins, 0)
        self.assertEqual(loser.cup_played, 1)
        self.assertEqual(loser.contests[-1], ("battlecup", -2))

        tied = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now)
        assert tied is not None
        self.assertEqual(await self.db.join_battlecup(tied, 1), "joined")
        self.assertEqual(await self.db.join_battlecup(tied, 2), "joined")
        self.assertEqual(len(await self.db.claim_due_battlecups(self.now)), 1)
        tied_outcome = await self.db.finish_battlecup(tied, {1: 4, 2: 4})
        assert tied_outcome is not None
        self.assertEqual(sorted(item.prize for item in tied_outcome.payouts), [2, 2])
        anna = await self.db.find_player(1, -100)
        boris = await self.db.find_player(2, -100)
        assert anna is not None and boris is not None
        self.assertEqual(anna.size, 22)
        self.assertEqual(boris.size, 18)

        split = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now)
        assert split is not None
        for user_id in (1, 2, 3):
            self.assertEqual(await self.db.join_battlecup(split, user_id), "joined")
        self.assertEqual(len(await self.db.claim_due_battlecups(self.now)), 1)
        split_outcome = await self.db.finish_battlecup(split, {1: 5, 2: 5, 3: 5})
        assert split_outcome is not None
        self.assertEqual({item.user_id: item.prize for item in split_outcome.payouts}, {1: 2, 2: 2, 3: 2})

        odd = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now)
        assert odd is not None
        for user_id in (1, 2, 3):
            self.assertEqual(await self.db.join_battlecup(odd, user_id), "joined")
        self.assertEqual(len(await self.db.claim_due_battlecups(self.now)), 1)
        odd_outcome = await self.db.finish_battlecup(odd, {1: 3, 2: 6, 3: 6})
        assert odd_outcome is not None
        prizes = {item.user_id: item.prize for item in odd_outcome.payouts}
        self.assertEqual(prizes, {1: 0, 2: 3, 3: 3})
        self.assertEqual(sum(prizes.values()), 6)

        short = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now)
        assert short is not None
        self.assertEqual(await self.db.join_battlecup(short, 1), "joined")
        before = await self.db.find_player(1, -100)
        assert before is not None
        self.assertEqual(len(await self.db.claim_due_battlecups(self.now)), 1)
        refunded = await self.db.refund_battlecup(short)
        assert refunded is not None
        self.assertIsNone(await self.db.refund_battlecup(short))
        after = await self.db.find_player(1, -100)
        assert after is not None
        self.assertEqual(after.size, before.size + 2)
        self.assertIsNotNone(await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=self.now))

    async def test_head_to_head_survives_old_contest_table(self) -> None:
        await self.db.set_size(
            user_id=1, chat_id=-100, username="ann", first_name="Анна", chat_title="Чат", size=20
        )
        await self.db.set_size(
            user_id=2, chat_id=-100, username="boris", first_name="Борис", chat_title="Чат", size=15
        )
        duel_id = await self.db.create_duel(chat_id=-100, challenger_id=1, opponent_id=2)
        assert duel_id is not None
        self.assertTrue(await self.db.claim_duel(duel_id, 2))
        await self.db.apply_duel(duel_id, 6, 2)
        await self.db.conn.execute("ALTER TABLE contest_results DROP COLUMN opponent_id")
        await self.db.conn.commit()
        path = self.db.path
        await self.db.close()
        self.db = Database(path, grow_min=-5, grow_max=10, zone=self.zone, clock=lambda: self.now)
        await self.db.connect()
        winner = await self.db.get_stats(1, -100)
        loser = await self.db.get_stats(2, -100)
        assert winner is not None and loser is not None
        self.assertEqual(winner.rivals, [("Борис", 1, 0, 0, 4)])
        self.assertEqual(loser.rivals, [("Анна", 0, 1, 0, -4)])

    async def test_ffa_duel_is_open_until_someone_accepts(self) -> None:
        await self.db.set_size(
            user_id=1, chat_id=-100, username="ann", first_name="Анна", chat_title="Чат", size=20
        )
        await self.db.set_size(
            user_id=2, chat_id=-100, username="boris", first_name="Борис", chat_title="Чат", size=15
        )
        duel_id = await self.db.create_ffa_duel(chat_id=-100, challenger_id=1)
        assert duel_id is not None
        self.assertIsNone(await self.db.create_ffa_duel(chat_id=-100, challenger_id=1))
        pending = await self.db.get_duel(duel_id)
        assert pending is not None
        self.assertIsNone(pending.opponent_id)
        self.assertFalse(await self.db.claim_duel(duel_id, 1))
        self.assertFalse(await self.db.claim_duel(duel_id, 3))
        self.assertFalse(await self.db.decline_duel(duel_id, 2))
        self.assertTrue(await self.db.claim_duel(duel_id, 2))
        self.assertFalse(await self.db.claim_duel(duel_id, 2))
        accepted = await self.db.get_duel(duel_id)
        assert accepted is not None
        self.assertEqual(accepted.opponent_id, 2)
        self.assertEqual(accepted.status, "rolling")
        outcome = await self.db.apply_duel(duel_id, 6, 2)
        assert outcome is not None
        self.assertEqual(outcome.challenger_size, 24)
        self.assertEqual(outcome.opponent_size, 11)
        again = await self.db.create_ffa_duel(chat_id=-100, challenger_id=1)
        assert again is not None
        self.assertTrue(await self.db.cancel_duel(again, 1))

    async def test_cleanup_keeps_pending_duels_and_long_battlecups(self) -> None:
        await self.db.set_size(
            user_id=1, chat_id=-100, username="ann", first_name="Анна", chat_title="Чат", size=20
        )
        await self.db.set_size(
            user_id=2, chat_id=-100, username="boris", first_name="Борис", chat_title="Чат", size=15
        )
        started = self.now
        await self.db.set_clear_minutes(-100, 20)
        await self.db.set_clear_minutes(-200, 20)
        await self.db.track_message(-100, 1)
        duel_id = await self.db.create_duel(chat_id=-100, challenger_id=1, opponent_id=2)
        assert duel_id is not None
        await self.db.hold_message(-100, 2, kind="duel", ref_id=duel_id)
        long_id = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=started + 3600)
        assert long_id is not None
        await self.db.hold_message(-100, 3, kind="battlecup", ref_id=long_id)
        short_id = await self.db.create_battlecup(chat_id=-200, stake=2, starts_at=started + 300)
        assert short_id is not None
        await self.db.hold_message(-200, 5, kind="battlecup", ref_id=short_id)

        self.now = started + 21 * 60
        await self.db.track_message(-100, 4)
        due = set(await self.db.due_bot_messages(self.now))
        self.assertIn((-100, 1), due)
        self.assertNotIn((-200, 5), due)
        self.assertNotIn((-100, 2), due)
        self.assertNotIn((-100, 3), due)
        self.assertNotIn((-100, 4), due)

        self.assertTrue(await self.db.claim_duel(duel_id, 2))
        started_cups = await self.db.claim_due_battlecups(self.now)
        self.assertEqual([cup.id for cup in started_cups], [short_id])
        due = set(await self.db.due_bot_messages(self.now))
        self.assertIn((-100, 2), due)
        self.assertIn((-200, 5), due)
        self.assertNotIn((-100, 3), due)

        await self.db.set_clear_minutes(-100, None)
        due = set(await self.db.due_bot_messages(self.now))
        self.assertNotIn((-100, 1), due)
        self.assertIn((-200, 5), due)

    async def test_old_duel_table_accepts_open_challenge(self) -> None:
        await self.db.close()
        path = Path(self._dir.name) / "legacy.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """
                CREATE TABLE duels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    challenger_id INTEGER NOT NULL,
                    opponent_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    challenger_roll INTEGER,
                    opponent_roll INTEGER,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO duels (chat_id, challenger_id, opponent_id, status, created_at)
                VALUES (-100, 1, 2, 'pending', 1)
                """
            )
            conn.commit()
        finally:
            conn.close()
        self.db = Database(path, grow_min=-5, grow_max=10, zone=self.zone, clock=lambda: self.now)
        await self.db.connect()
        await self.db.set_size(
            user_id=1, chat_id=-100, username="ann", first_name="Анна", chat_title="Чат", size=20
        )
        await self.db.set_size(
            user_id=2, chat_id=-100, username="boris", first_name="Борис", chat_title="Чат", size=15
        )
        old = await self.db.get_duel(1)
        assert old is not None
        self.assertEqual(old.opponent_id, 2)
        ffa_id = await self.db.create_ffa_duel(chat_id=-100, challenger_id=1)
        assert ffa_id is not None
        self.assertTrue(await self.db.claim_duel(ffa_id, 2))

    async def test_review_keeps_this_chat_and_this_period(self) -> None:
        sunday = int(datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp())
        thursday = int(datetime(2026, 10, 29, 12, tzinfo=timezone.utc).timestamp())
        friday = int(datetime(2026, 10, 30, 12, tzinfo=timezone.utc).timestamp())

        self.now = sunday
        await self.db.try_grow(
            user_id=1, chat_id=-100, username="ann", first_name="Анна", chat_title="Чат", rng=lambda: 4
        )
        self.now = thursday
        await self.db.try_grow(
            user_id=2, chat_id=-100, username="boris", first_name="Борис", chat_title="Чат", rng=lambda: -2
        )
        self.now = friday
        await self.db.try_grow(
            user_id=1, chat_id=-100, username="ann", first_name="Анна", chat_title="Чат", rng=lambda: 10
        )
        await self.db.try_grow(
            user_id=1, chat_id=-200, username="ann", first_name="Анна", chat_title="Другой", rng=lambda: 8
        )
        duel_id = await self.db.create_duel(chat_id=-100, challenger_id=1, opponent_id=2)
        assert duel_id is not None
        self.assertTrue(await self.db.claim_duel(duel_id, 2))
        self.assertIsNotNone(await self.db.apply_duel(duel_id, 6, 2))
        cup_id = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=friday)
        assert cup_id is not None
        self.assertEqual(await self.db.join_battlecup(cup_id, 1), "joined")
        self.assertEqual(await self.db.join_battlecup(cup_id, 2), "joined")
        self.assertEqual(len(await self.db.claim_due_battlecups(friday)), 1)
        self.assertIsNotNone(await self.db.finish_battlecup(cup_id, {1: 6, 2: 1}))

        day = await self.db.chat_review(-100, week=False)
        self.assertEqual(day.plays, 1)
        self.assertEqual([(item.name, item.delta, item.size_after) for item in day.measures], [("Анна", 10, 14)])
        self.assertEqual(day.play_sum, 10)
        assert day.best_growth is not None
        self.assertEqual((day.best_growth.name, day.best_growth.value), ("Анна", 10))
        self.assertIsNone(day.worst_growth)
        self.assertEqual(day.duels, 1)
        assert day.duel_haul is not None
        self.assertEqual((day.duel_haul.winner, day.duel_haul.loser, day.duel_haul.amount), ("Анна", "Борис", 4))
        self.assertEqual(day.cups, 1)
        assert day.cup_sponsor is not None
        self.assertEqual(day.cup_sponsor.name, "Борис")
        self.assertLess(day.cup_sponsor.value, 0)

        week = await self.db.chat_review(-100, week=True)
        self.assertEqual(week.plays, 2)
        self.assertEqual(
            [(item.name, item.delta, item.size_after) for item in week.measures],
            [("Борис", -2, -2), ("Анна", 10, 14)],
        )
        self.assertEqual(week.play_sum, 8)
        assert week.worst_growth is not None
        self.assertEqual((week.worst_growth.name, week.worst_growth.value), ("Борис", -2))
        self.assertEqual(week.duels, 1)
        self.assertEqual(week.cups, 1)

    async def test_day_lists_today_and_folds_duels(self) -> None:
        thursday = int(datetime(2026, 10, 29, 12, tzinfo=timezone.utc).timestamp())
        friday = int(datetime(2026, 10, 30, 12, tzinfo=timezone.utc).timestamp())
        self.now = thursday
        await self.db.try_grow(
            user_id=1, chat_id=-100, username="ann", first_name="Анна", chat_title="Чат", rng=lambda: 4
        )
        self.now = friday
        await self.db.try_grow(
            user_id=1, chat_id=-100, username="ann", first_name="Анна", chat_title="Чат", rng=lambda: 10
        )
        await self.db.try_grow(
            user_id=2, chat_id=-100, username="boris", first_name="Борис", chat_title="Чат", rng=lambda: -3
        )
        await self.db.try_grow(
            user_id=1, chat_id=-200, username="ann", first_name="Анна", chat_title="Другой", rng=lambda: 8
        )
        first = await self.db.create_duel(chat_id=-100, challenger_id=1, opponent_id=2)
        assert first is not None
        self.assertTrue(await self.db.claim_duel(first, 2))
        self.assertIsNotNone(await self.db.apply_duel(first, 6, 2))
        second = await self.db.create_duel(chat_id=-100, challenger_id=2, opponent_id=1)
        assert second is not None
        self.assertTrue(await self.db.claim_duel(second, 1))
        self.assertIsNotNone(await self.db.apply_duel(second, 4, 4))
        cup_id = await self.db.create_battlecup(chat_id=-100, stake=2, starts_at=friday)
        assert cup_id is not None
        self.assertEqual(await self.db.join_battlecup(cup_id, 1), "joined")
        self.assertEqual(await self.db.join_battlecup(cup_id, 2), "joined")
        self.assertEqual(len(await self.db.claim_due_battlecups(friday)), 1)
        self.assertIsNotNone(await self.db.finish_battlecup(cup_id, {1: 6, 2: 1}))

        report = await self.db.chat_day(-100)
        self.assertEqual([(item.name, item.delta) for item in report.measures], [("Анна", 10), ("Борис", -3)])
        self.assertEqual(report.duels, (DuelScore("Анна", "Борис", 1, 0, 1, 4),))
        self.assertEqual(len(report.cups), 1)
        self.assertEqual(report.cups[0], CupStanding("Анна", 1, 4))

        week = await self.db.chat_day(-100, week=True)
        self.assertEqual(
            [(item.name, item.delta) for item in week.measures],
            [("Анна", 4), ("Анна", 10), ("Борис", -3)],
        )
        self.assertEqual(week.duels, report.duels)
        self.assertEqual(week.cups, report.cups)


class CardTests(unittest.TestCase):
    def test_png(self) -> None:
        png = render_stats(
            CardData(
                name="Анна (@ann)",
                chat_title="Тестовый чат",
                size=27,
                rank=3,
                total=18,
                plays=12,
                best_gain=10,
                worst_loss=-5,
                next_free_at=1_700_003_600,
                now=1_700_000_000,
                deltas=[4, -2, 8, 0, -5, 10, 3, -1],
                duel_wins=2,
                duel_losses=1,
                cup_wins=1,
                cup_played=2,
                contests=(("duel", 4), ("battlecup", -2), ("duel", 0)),
                rivals=(("Борис", 2, 1, 0, 6), ("Виктор", 0, 1, 1, -3)),
            )
        )
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 5000)
        self.assertGreater(Image.open(BytesIO(png)).height, 900)

    def test_top_picture(self) -> None:
        png = render_top(
            [
                ("Анна", 30),
                ("Борис", 18),
                ("Виктор", 4),
                ("Глеб", 0),
                ("Дина", -6),
            ]
        )
        self.assertTrue(png.startswith(b"\x89PNG"))
        image = Image.open(BytesIO(png))
        self.assertEqual(image.size, (1200, 680))

    def test_top_picture_heights_follow_the_sizes(self) -> None:
        image = Image.open(BytesIO(render_top([("Анна", 20), ("Борис", 10)])))
        heights = _mint_heights(image)
        self.assertEqual(len(heights), 2)
        self.assertAlmostEqual(heights[0] / heights[1], 2, delta=0.08)

    def test_global_top_picture_keeps_the_same_scale(self) -> None:
        rows = [("Анна", 20), ("Борис", 10)]
        chat = render_top(rows)
        global_pic = render_top(rows, title="ГЛОБАЛЬНЫЙ ТОП")
        self.assertNotEqual(chat, global_pic)
        image = Image.open(BytesIO(global_pic))
        self.assertEqual(image.size, (1200, 680))
        heights = _mint_heights(image)
        self.assertEqual(len(heights), 2)
        self.assertAlmostEqual(heights[0] / heights[1], 2, delta=0.08)

    def test_cyrillic_glyphs(self) -> None:
        font = _font(40, bold=True)
        cyrillic = bytes(font.getmask("ЛИНЕЙКА"))
        missing = bytes(font.getmask("\ue000"))
        self.assertNotEqual(cyrillic, missing)
        self.assertGreater(sum(cyrillic), 0)


class TelegramErrorTests(unittest.IsolatedAsyncioTestCase):
    def test_flood_text_includes_the_wait_and_telegram_reason(self) -> None:
        error = _retry_after(35)
        text = describe_telegram_error(error)
        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("35 секунд", text)
        self.assertIn("Too Many Requests: retry after 35", text)

    def test_bad_request_text_includes_telegram_reason(self) -> None:
        error = TelegramBadRequest(_Method(), "Bad Request: message is not modified")
        text = describe_telegram_error(error)
        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("Telegram не принял запрос", text)
        self.assertIn("message is not modified", text)

    def test_other_errors_stay_without_telegram_text(self) -> None:
        self.assertIsNone(describe_telegram_error(RuntimeError("кубик")))
        self.assertEqual(notice_with_cause("Кубики не бросились.", RuntimeError("кубик")), "Кубики не бросились.")

    def test_delivery_target_uses_the_command_message(self) -> None:
        update = SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=7),
            edited_message=None,
            channel_post=None,
            callback_query=None,
            my_chat_member=None,
            chat_member=None,
        )
        self.assertEqual(delivery_target(update), (-100, 7))

    async def test_flood_notice_waits_before_the_first_send(self) -> None:
        bot = _RecordingBot()
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        with patch("pipisa.telegram_errors.asyncio.sleep", fake_sleep):
            sent = await tell_user(bot, -100, "Броски не удались. Взносы вернулись.", _retry_after(35))
        self.assertTrue(sent)
        self.assertEqual(slept, [35])
        self.assertEqual(bot.texts, ["Броски не удались. Взносы вернулись."])

    async def test_command_error_is_delivered_to_the_chat(self) -> None:
        bot = _RecordingBot()
        update = SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=4),
            edited_message=None,
            channel_post=None,
            callback_query=None,
            my_chat_member=None,
            chat_member=None,
        )
        event = SimpleNamespace(exception=TelegramBadRequest(_Method(), "Bad Request: chat not found"), update=update)
        await on_telegram_error(event, bot)
        self.assertEqual(len(bot.texts), 1)
        self.assertIn("chat not found", bot.texts[0])
        self.assertEqual(bot.replies, [4])

    async def test_flood_during_a_round_waits_and_continues(self) -> None:
        bot = _CupBot()
        db = _CupDb()
        cup = Battlecup(
            id=1,
            chat_id=-100,
            status="running",
            stake=2,
            starts_at=0,
            message_id=9,
            players=[
                BattlecupPlayer(1, "Анна", None, None),
                BattlecupPlayer(2, "Борис", None, None),
            ],
        )
        slept: list[float] = []

        async def tracked_sleep(seconds: float) -> None:
            slept.append(seconds)

        with patch("asyncio.sleep", tracked_sleep):
            await play_battlecup(bot, db, cup)

        self.assertFalse(db.refunded)
        self.assertEqual(db.totals, {1: 20, 2: 20})
        self.assertIn(35, slept)
        self.assertEqual(bot.dice_attempts, 11)

    async def test_darts_scores_the_board_not_the_animation_number(self) -> None:
        bot = _CupBot()
        db = _CupDb()
        cup = Battlecup(
            id=2,
            chat_id=-100,
            status="running",
            stake=2,
            starts_at=0,
            message_id=9,
            players=[
                BattlecupPlayer(1, "Анна", None, None),
                BattlecupPlayer(2, "Борис", None, None),
            ],
            kind="darts",
        )

        async def tracked_sleep(_seconds: float) -> None:
            return None

        with patch("asyncio.sleep", tracked_sleep):
            await play_battlecup(bot, db, cup)

        self.assertEqual(db.totals, {1: 15, 2: 15})
        self.assertTrue(bot.emojis)
        self.assertTrue(all(emoji == "🎯" for emoji in bot.emojis))


class _CupDb:
    def __init__(self) -> None:
        self.refunded = False
        self.totals: dict[int, int] | None = None

    async def refund_battlecup(self, battlecup_id: int) -> None:
        self.refunded = True

    async def finish_battlecup(self, battlecup_id: int, totals: dict[int, int]) -> None:
        self.totals = dict(totals)


class _CupBot:
    def __init__(self) -> None:
        self.dice_attempts = 0
        self.emojis: list[str] = []
        self._limited = False

    async def send_message(self, chat_id: int, text: str, reply_markup: object = None) -> SimpleNamespace:
        return SimpleNamespace(message_id=1, dice=None)

    async def send_dice(self, chat_id: int, emoji: str) -> SimpleNamespace:
        self.dice_attempts += 1
        self.emojis.append(emoji)
        if self.dice_attempts == 2 and not self._limited:
            self._limited = True
            raise _retry_after(35)
        return SimpleNamespace(message_id=self.dice_attempts, dice=SimpleNamespace(value=4))

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        reply_markup: object = None,
    ) -> None:
        return None

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        return None


class _Method:
    chat_id = -100


class _RecordingBot:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.replies: list[int | None] = []

    async def send_message(self, chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
        self.texts.append(text)
        self.replies.append(reply_to_message_id)


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(_Method(), f"Too Many Requests: retry after {seconds}", seconds)


def _mint_heights(image: Image.Image) -> list[int]:
    pixels = image.load()
    assert pixels is not None
    columns = []
    for x in range(image.width):
        span = 0
        for y in range(image.height):
            if pixels[x, y][:3] == (72, 214, 180):
                span += 1
        columns.append(span)
    heights = []
    current = 0
    for span in columns:
        if span:
            current = max(current, span)
        elif current:
            heights.append(current)
            current = 0
    if current:
        heights.append(current)
    return heights


if __name__ == "__main__":
    unittest.main()
