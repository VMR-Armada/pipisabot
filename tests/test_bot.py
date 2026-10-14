from __future__ import annotations

import tempfile
import unittest
from datetime import timezone
from pathlib import Path

from pipisa.day import next_midnight

from pipisa.db import Database
from pipisa.stats_card import CardData, _font, render_stats
from pipisa.texts import format_wait, plural
from pipisa.transfer import ImportItem, parse_import_text, parse_size


class TextTests(unittest.TestCase):
    def test_plural(self) -> None:
        self.assertEqual(plural(1, "попытка", "попытки", "попыток"), "попытка")
        self.assertEqual(plural(2, "попытка", "попытки", "попыток"), "попытки")
        self.assertEqual(plural(5, "попытка", "попытки", "попыток"), "попыток")
        self.assertEqual(plural(11, "попытка", "попытки", "попыток"), "попыток")
        self.assertEqual(plural(21, "час", "часа", "часов"), "час")

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

        global_entries, global_standing = await self.db.global_top(1)
        self.assertEqual([entry.user_id for entry in global_entries], [2, 1])
        self.assertEqual(global_entries[1].size, 8)
        self.assertEqual(global_entries[1].chat_title, "Другой")
        assert global_standing is not None
        self.assertEqual(global_standing.size, 8)
        self.assertEqual(global_standing.rank, 2)

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
        self.assertIsNone(await self.db.apply_duel(duel_id, 6, 2))

        tie_id = await self.db.create_duel(chat_id=-100, challenger_id=2, opponent_id=1)
        assert tie_id is not None
        self.assertTrue(await self.db.decline_duel(tie_id, 1))
        anna = await self.db.find_player(1, -100)
        boris = await self.db.find_player(2, -100)
        assert anna is not None and boris is not None
        self.assertEqual(anna.size, 24)
        self.assertEqual(boris.size, 11)


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
            )
        )
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 5000)

    def test_cyrillic_glyphs(self) -> None:
        font = _font(40, bold=True)
        cyrillic = bytes(font.getmask("ЛИНЕЙКА"))
        missing = bytes(font.getmask("\ue000"))
        self.assertNotEqual(cyrillic, missing)
        self.assertGreater(sum(cyrillic), 0)


if __name__ == "__main__":
    unittest.main()
