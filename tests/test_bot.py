from __future__ import annotations

from io import BytesIO
import tempfile
import unittest
from unittest.mock import patch
from datetime import timezone
from pathlib import Path

from PIL import Image

from pipisa.battlecup import countdown_seconds
from pipisa.day import next_midnight

from pipisa.db import BattlecupOutcome, BattlecupPayout, Database, Entry, prize_shares
from pipisa.handlers import _rows
from pipisa.stats_card import CardData, _font, render_stats
from pipisa.texts import battlecup_result, duel_refusal, format_wait, plural, top_message
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
        self.assertIn("Анна, кегли сегодня были на твоей стороне.", text)
        self.assertIn("Вова, шар увидел кегли и сделал вид, что их нет.", text)
        self.assertNotIn("Борис, шар", text)

    def test_battlecup_countdown_steps_once_a_minute(self) -> None:
        start = 1_000
        self.assertEqual(countdown_seconds(start + 300, start), 300)
        self.assertEqual(countdown_seconds(start + 300, start + 59), 300)
        self.assertEqual(countdown_seconds(start + 300, start + 60), 240)
        self.assertEqual(countdown_seconds(start + 300, start + 299), 60)
        self.assertEqual(countdown_seconds(start + 300, start + 300), 0)

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
        self.assertEqual(winner.rivals, [("Борис", 1, 0, 0)])
        self.assertEqual(loser.duel_losses, 1)
        self.assertEqual(loser.contests, [("duel", -4)])
        self.assertEqual(loser.rivals, [("Анна", 0, 1, 0)])
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
        self.assertEqual(winner.rivals, [("Борис", 1, 0, 0)])
        self.assertEqual(loser.rivals, [("Анна", 0, 1, 0)])


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
                rivals=(("Борис", 2, 1, 0), ("Виктор", 0, 1, 1)),
            )
        )
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 5000)
        self.assertGreater(Image.open(BytesIO(png)).height, 900)

    def test_cyrillic_glyphs(self) -> None:
        font = _font(40, bold=True)
        cyrillic = bytes(font.getmask("ЛИНЕЙКА"))
        missing = bytes(font.getmask("\ue000"))
        self.assertNotEqual(cyrillic, missing)
        self.assertGreater(sum(cyrillic), 0)


if __name__ == "__main__":
    unittest.main()
