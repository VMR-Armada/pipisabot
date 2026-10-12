from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from pipisa.transfer import ImportItem

Clock = Callable[[], int]
Rng = Callable[[], int]


@dataclass
class GrowResult:
    ok: bool
    size: int
    wait_seconds: int = 0
    delta: int = 0


@dataclass
class Entry:
    user_id: int
    chat_id: int
    size: int
    username: str | None
    first_name: str
    chat_title: str | None


@dataclass
class Standing:
    rank: int
    total: int
    size: int


@dataclass
class Stats:
    size: int
    rank: int
    total: int
    plays: int
    best_gain: int
    worst_loss: int
    next_free_at: int | None
    deltas: list[int]
    first_name: str
    username: str | None
    chat_title: str | None


class Database:
    def __init__(
        self,
        path: Path | str,
        *,
        grow_min: int,
        grow_max: int,
        cooldown_seconds: int,
        clock: Clock | None = None,
    ) -> None:
        self.path = str(path)
        self.grow_min = grow_min
        self.grow_max = grow_max
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or (lambda: int(time.time()))
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                next_free_at INTEGER,
                plays INTEGER NOT NULL DEFAULT 0,
                best_gain INTEGER NOT NULL DEFAULT 0,
                worst_loss INTEGER NOT NULL DEFAULT 0,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                chat_title TEXT,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS plays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                size_after INTEGER NOT NULL,
                played_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_plays_user_chat
                ON plays(user_id, chat_id, id);
            CREATE INDEX IF NOT EXISTS idx_players_chat_size
                ON players(chat_id, size DESC);

            CREATE TABLE IF NOT EXISTS imported (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                name_key TEXT NOT NULL,
                first_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                chat_title TEXT,
                PRIMARY KEY (chat_id, username, name_key)
            );
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("База не открыта")
        return self._db

    async def try_grow(
        self,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str,
        chat_title: str | None,
        rng: Rng | None = None,
    ) -> GrowResult:
        roll = rng or (lambda: random.randint(self.grow_min, self.grow_max))
        async with self._lock:
            try:
                await self._upsert_player(
                    user_id=user_id,
                    chat_id=chat_id,
                    username=username,
                    first_name=first_name,
                    chat_title=chat_title,
                )
                row = await self._get_row(user_id, chat_id)
                if row is None:
                    raise RuntimeError("Игрок не создался")
                now = self._clock()
                next_free_at = row["next_free_at"]
                if next_free_at is not None and now < int(next_free_at):
                    await self.conn.commit()
                    return GrowResult(
                        ok=False,
                        size=int(row["size"]),
                        wait_seconds=int(next_free_at) - now,
                    )

                scheduled = now + self.cooldown_seconds
                delta = int(roll())
                if delta < self.grow_min or delta > self.grow_max:
                    raise ValueError("Случайное изменение вне допустимого диапазона")
                size = int(row["size"]) + delta
                best_gain = int(row["best_gain"])
                worst_loss = int(row["worst_loss"])
                if delta > 0:
                    best_gain = max(best_gain, delta)
                elif delta < 0:
                    worst_loss = min(worst_loss, delta)

                await self.conn.execute(
                    """
                    UPDATE players
                    SET size = ?, next_free_at = ?,
                        plays = plays + 1, best_gain = ?, worst_loss = ?
                    WHERE user_id = ? AND chat_id = ?
                    """,
                    (size, scheduled, best_gain, worst_loss, user_id, chat_id),
                )
                await self.conn.execute(
                    """
                    INSERT INTO plays (user_id, chat_id, delta, size_after, played_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, chat_id, delta, size, now),
                )
                await self.conn.commit()
                return GrowResult(
                    ok=True,
                    size=size,
                    wait_seconds=max(0, scheduled - now),
                    delta=delta,
                )
            except Exception:
                await self.conn.rollback()
                raise

    async def touch_chat(self, chat_id: int, title: str | None) -> None:
        if not title:
            return
        async with self._lock:
            await self.conn.execute(
                "UPDATE players SET chat_title = ? WHERE chat_id = ?",
                (title, chat_id),
            )
            await self.conn.commit()

    async def get_stats(self, user_id: int, chat_id: int) -> Stats | None:
        async with self._lock:
            row = await self._get_row(user_id, chat_id)
            if row is None or int(row["plays"]) <= 0:
                return None
            board = await self._chat_board(chat_id)
            standing = _standing_for(board, user_id, row["username"], row["first_name"])
            if standing is None:
                standing = Standing(rank=1, total=1, size=int(row["size"]))
            cursor = await self.conn.execute(
                """
                SELECT delta FROM plays
                WHERE user_id = ? AND chat_id = ?
                ORDER BY id DESC
                LIMIT 12
                """,
                (user_id, chat_id),
            )
            deltas = [int(item["delta"]) for item in await cursor.fetchall()]
            deltas.reverse()
            return Stats(
                size=int(row["size"]),
                rank=standing.rank,
                total=standing.total,
                plays=int(row["plays"]),
                best_gain=int(row["best_gain"]),
                worst_loss=int(row["worst_loss"]),
                next_free_at=row["next_free_at"],
                deltas=deltas,
                first_name=row["first_name"],
                username=row["username"],
                chat_title=row["chat_title"],
            )

    async def set_size(
        self,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str,
        chat_title: str | None,
        size: int,
    ) -> None:
        async with self._lock:
            try:
                await self._upsert_player(
                    user_id=user_id,
                    chat_id=chat_id,
                    username=username,
                    first_name=first_name,
                    chat_title=chat_title,
                )
                await self.conn.execute(
                    """
                    UPDATE players
                    SET size = ?,
                        plays = CASE WHEN plays < 1 THEN 1 ELSE plays END
                    WHERE user_id = ? AND chat_id = ?
                    """,
                    (size, user_id, chat_id),
                )
                await self._delete_import(chat_id, username, first_name)
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def import_sizes(
        self,
        *,
        chat_id: int,
        chat_title: str | None,
        items: list[ImportItem],
    ) -> tuple[list[ImportItem], list[ImportItem]]:
        applied: list[ImportItem] = []
        pending: list[ImportItem] = []
        async with self._lock:
            try:
                players = await self._players_in_chat(chat_id)
                for item in items:
                    match = _match_player(players, item)
                    await self._delete_import(chat_id, item.username, item.first_name)
                    if match is not None:
                        await self.conn.execute(
                            """
                            UPDATE players
                            SET size = ?,
                                plays = CASE WHEN plays < 1 THEN 1 ELSE plays END,
                                chat_title = COALESCE(?, chat_title)
                            WHERE user_id = ? AND chat_id = ?
                            """,
                            (item.size, chat_title, int(match["user_id"]), chat_id),
                        )
                        applied.append(item)
                        continue
                    await self.conn.execute(
                        """
                        INSERT INTO imported (chat_id, username, name_key, first_name, size, chat_title)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chat_id,
                            item.username or "",
                            _name_key(item.first_name),
                            item.first_name,
                            item.size,
                            chat_title,
                        ),
                    )
                    pending.append(item)
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        return applied, pending

    async def claim_import(
        self,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str,
        chat_title: str | None,
    ) -> bool:
        async with self._lock:
            try:
                row = await self._find_import(chat_id, username, first_name)
                if row is None:
                    return False
                player = await self._get_row(user_id, chat_id)
                if player is not None and int(player["plays"]) > 0:
                    await self.conn.commit()
                    return False
                await self._upsert_player(
                    user_id=user_id,
                    chat_id=chat_id,
                    username=username,
                    first_name=first_name,
                    chat_title=chat_title,
                )
                await self.conn.execute(
                    """
                    UPDATE players
                    SET size = ?,
                        plays = CASE WHEN plays < 1 THEN 1 ELSE plays END
                    WHERE user_id = ? AND chat_id = ?
                    """,
                    (int(row["size"]), user_id, chat_id),
                )
                await self._delete_import_row(int(row["chat_id"]), row["username"], row["name_key"])
                await self.conn.commit()
                return True
            except Exception:
                await self.conn.rollback()
                raise

    async def chat_top(
        self,
        chat_id: int,
        user_id: int,
        limit: int = 10,
        username: str | None = None,
        first_name: str | None = None,
    ) -> tuple[list[Entry], Standing | None]:
        async with self._lock:
            board = await self._chat_board(chat_id)
            standing = _standing_for(board, user_id, username, first_name)
            return board[:limit], standing

    async def global_top(
        self, user_id: int, limit: int = 10
    ) -> tuple[list[Entry], Standing | None]:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        user_id, chat_id, size, username, first_name, chat_title, plays,
                        ROW_NUMBER() OVER (
                            PARTITION BY user_id
                            ORDER BY size DESC, plays DESC, chat_id
                        ) AS rn
                    FROM players
                    WHERE plays > 0
                )
                SELECT user_id, chat_id, size, username, first_name, chat_title
                FROM ranked
                WHERE rn = 1
                ORDER BY size DESC, plays DESC, user_id
                LIMIT ?
                """,
                (limit,),
            )
            entries = [_entry(row) for row in await cursor.fetchall()]
            standing = await self._global_standing(user_id)
            return entries, standing

    async def _upsert_player(
        self,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str,
        chat_title: str | None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO players (
                user_id, chat_id, size, plays, best_gain, worst_loss,
                username, first_name, chat_title, created_at
            )
            VALUES (?, ?, 0, 0, 0, 0, ?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                chat_title = COALESCE(excluded.chat_title, players.chat_title)
            """,
            (user_id, chat_id, username, first_name or "Игрок", chat_title, self._clock()),
        )

    async def _get_row(self, user_id: int, chat_id: int) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(
            "SELECT * FROM players WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        return await cursor.fetchone()

    async def _chat_board(self, chat_id: int) -> list[Entry]:
        cursor = await self.conn.execute(
            """
            SELECT user_id, chat_id, size, username, first_name, chat_title
            FROM players
            WHERE chat_id = ? AND plays > 0
            """,
            (chat_id,),
        )
        board = [_entry(row) for row in await cursor.fetchall()]
        taken_usernames = {(entry.username or "").lower() for entry in board if entry.username}
        taken_names = {entry.first_name.casefold() for entry in board}
        cursor = await self.conn.execute(
            """
            SELECT first_name, username, size, chat_title
            FROM imported
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        for row in await cursor.fetchall():
            username = row["username"] or None
            if username and username.lower() in taken_usernames:
                continue
            if not username and row["first_name"].casefold() in taken_names:
                continue
            board.append(
                Entry(
                    user_id=0,
                    chat_id=chat_id,
                    size=int(row["size"]),
                    username=username,
                    first_name=row["first_name"],
                    chat_title=row["chat_title"],
                )
            )
        board.sort(key=lambda entry: (-entry.size, entry.first_name.casefold(), entry.user_id))
        return board

    async def _players_in_chat(self, chat_id: int) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute("SELECT * FROM players WHERE chat_id = ?", (chat_id,))
        return list(await cursor.fetchall())

    async def _delete_import(self, chat_id: int, username: str | None, first_name: str) -> None:
        if username:
            await self.conn.execute(
                "DELETE FROM imported WHERE chat_id = ? AND username = ?",
                (chat_id, username.lower()),
            )
            return
        await self.conn.execute(
            "DELETE FROM imported WHERE chat_id = ? AND username = '' AND name_key = ?",
            (chat_id, _name_key(first_name)),
        )

    async def _delete_import_row(self, chat_id: int, username: str, name_key: str) -> None:
        await self.conn.execute(
            "DELETE FROM imported WHERE chat_id = ? AND username = ? AND name_key = ?",
            (chat_id, username, name_key),
        )

    async def _find_import(
        self, chat_id: int, username: str | None, first_name: str
    ) -> aiosqlite.Row | None:
        if username:
            cursor = await self.conn.execute(
                """
                SELECT * FROM imported
                WHERE chat_id = ? AND username = ?
                ORDER BY size DESC
                LIMIT 1
                """,
                (chat_id, username.lower()),
            )
            row = await cursor.fetchone()
            if row is not None:
                return row
        cursor = await self.conn.execute(
            """
            SELECT * FROM imported
            WHERE chat_id = ? AND username = '' AND name_key = ?
            """,
            (chat_id, _name_key(first_name)),
        )
        rows = await cursor.fetchall()
        if len(rows) == 1:
            return rows[0]
        return None

    async def _global_standing(self, user_id: int) -> Standing | None:
        cursor = await self.conn.execute(
            """
            WITH best AS (
                SELECT user_id, MAX(size) AS size
                FROM players
                WHERE plays > 0
                GROUP BY user_id
            )
            SELECT
                (SELECT size FROM best WHERE user_id = ?) AS size,
                (
                    SELECT COUNT(*) + 1 FROM best
                    WHERE size > (SELECT size FROM best WHERE user_id = ?)
                ) AS rank,
                (SELECT COUNT(*) FROM best) AS total
            """,
            (user_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None or row["size"] is None:
            return None
        return Standing(rank=int(row["rank"]), total=int(row["total"]), size=int(row["size"]))


def _name_key(name: str) -> str:
    return name.casefold().strip()


def _match_player(players: list[aiosqlite.Row], item: ImportItem) -> aiosqlite.Row | None:
    if item.username:
        for row in players:
            if (row["username"] or "").lower() == item.username:
                return row
        return None
    matches = [row for row in players if row["first_name"].casefold() == item.first_name.casefold()]
    if len(matches) == 1:
        return matches[0]
    return None


def _standing_for(
    board: list[Entry],
    user_id: int,
    username: str | None,
    first_name: str | None,
) -> Standing | None:
    viewer = next((entry for entry in board if entry.user_id == user_id and entry.user_id != 0), None)
    if viewer is None and username:
        viewer = next(
            (entry for entry in board if (entry.username or "").lower() == username.lower()),
            None,
        )
    if viewer is None and first_name:
        by_name = [
            entry
            for entry in board
            if entry.user_id == 0 and not entry.username and entry.first_name.casefold() == first_name.casefold()
        ]
        if len(by_name) == 1:
            viewer = by_name[0]
    if viewer is None:
        return None
    rank = 1
    for entry in board:
        if entry is viewer:
            continue
        if entry.size > viewer.size or (
            entry.size == viewer.size and entry.first_name.casefold() < viewer.first_name.casefold()
        ):
            rank += 1
    return Standing(rank=rank, total=len(board), size=viewer.size)


def _entry(row: aiosqlite.Row) -> Entry:
    return Entry(
        user_id=int(row["user_id"]),
        chat_id=int(row["chat_id"]),
        size=int(row["size"]),
        username=row["username"],
        first_name=row["first_name"],
        chat_title=row["chat_title"],
    )
