from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path

import aiosqlite

from pipisa.day import can_play, next_midnight, period_start, seconds_until_next_day
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
class Duel:
    id: int
    chat_id: int
    challenger_id: int
    opponent_id: int | None
    status: str
    challenger_name: str
    challenger_username: str | None
    opponent_name: str
    opponent_username: str | None


@dataclass
class DuelOutcome:
    challenger_roll: int
    opponent_roll: int
    diff: int
    challenger_size: int
    opponent_size: int
    challenger_name: str
    challenger_username: str | None
    opponent_name: str
    opponent_username: str | None


@dataclass
class BattlecupPlayer:
    user_id: int
    first_name: str
    username: str | None
    roll: int | None


@dataclass
class Battlecup:
    id: int
    chat_id: int
    status: str
    stake: int
    starts_at: int
    message_id: int | None
    players: list[BattlecupPlayer]


@dataclass
class BattlecupPayout:
    user_id: int
    first_name: str
    username: str | None
    roll: int
    prize: int
    size: int


@dataclass
class BattlecupOutcome:
    pool: int
    payouts: list[BattlecupPayout]


@dataclass(frozen=True)
class NamedValue:
    name: str
    value: int


@dataclass(frozen=True)
class Measure:
    name: str
    delta: int
    size_after: int
    played_at: int


@dataclass(frozen=True)
class DuelHaul:
    winner: str
    loser: str
    amount: int


@dataclass(frozen=True)
class PeriodReview:
    plays: int
    play_sum: int
    best_growth: NamedValue | None
    worst_growth: NamedValue | None
    best_measure: NamedValue | None
    worst_measure: NamedValue | None
    duels: int
    duel_leader: NamedValue | None
    duel_haul: DuelHaul | None
    cups: int
    cup_leader: NamedValue | None
    cup_sponsor: NamedValue | None
    measures: tuple[Measure, ...] = ()


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
    contests: list[tuple[str, int]]
    duel_wins: int
    duel_losses: int
    duel_ties: int
    cup_wins: int
    cup_played: int
    rivals: list[tuple[str, int, int, int, int]]
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
        zone: tzinfo,
        clock: Clock | None = None,
    ) -> None:
        self.path = str(path)
        self.grow_min = grow_min
        self.grow_max = grow_max
        self.zone = zone
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

            CREATE TABLE IF NOT EXISTS duels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                challenger_id INTEGER NOT NULL,
                opponent_id INTEGER,
                status TEXT NOT NULL,
                challenger_roll INTEGER,
                opponent_roll INTEGER,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS battlecups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                stake INTEGER NOT NULL,
                starts_at INTEGER NOT NULL,
                message_id INTEGER,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS battlecup_entries (
                battlecup_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                roll INTEGER,
                joined_at INTEGER NOT NULL,
                PRIMARY KEY (battlecup_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS contest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                place INTEGER NOT NULL,
                played_at INTEGER NOT NULL,
                opponent_id INTEGER,
                UNIQUE(kind, source_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS imported (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                name_key TEXT NOT NULL,
                first_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                chat_title TEXT,
                PRIMARY KEY (chat_id, username, name_key)
            );

            CREATE TABLE IF NOT EXISTS chat_cleanup (
                chat_id INTEGER PRIMARY KEY,
                minutes INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'plain',
                ref_id INTEGER,
                PRIMARY KEY (chat_id, message_id)
            );
            """
        )
        await self._ensure_nullable_duel_opponent()
        await self._ensure_contest_opponent()
        await self._backfill_contests()
        await self._db.commit()

    async def close(self) -> None:
        if self._db is None:
            return
        with contextlib.suppress(Exception):
            await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
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
                last_played = await self._last_played_at(user_id, chat_id)
                if not can_play(now, last_played, self.zone):
                    await self.conn.commit()
                    return GrowResult(
                        ok=False,
                        size=int(row["size"]),
                        wait_seconds=seconds_until_next_day(now, self.zone),
                    )

                scheduled = next_midnight(now, self.zone)
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
                ORDER BY id
                """,
                (user_id, chat_id),
            )
            all_deltas = [int(item["delta"]) for item in await cursor.fetchall()]
            best_gain, worst_loss = play_streaks(all_deltas)
            contests = await self._contest_rows(user_id, chat_id)
            summary = _contest_summary(contests)
            last_played = await self._last_played_at(user_id, chat_id)
            now = self._clock()
            available_at = None if can_play(now, last_played, self.zone) else next_midnight(now, self.zone)
            return Stats(
                size=int(row["size"]),
                rank=standing.rank,
                total=standing.total,
                plays=int(row["plays"]),
                best_gain=best_gain,
                worst_loss=worst_loss,
                next_free_at=available_at,
                deltas=all_deltas[-12:],
                contests=[(kind, delta) for kind, delta, _place in contests[-6:]],
                duel_wins=summary[0],
                duel_losses=summary[1],
                duel_ties=summary[2],
                cup_wins=summary[3],
                cup_played=summary[4],
                rivals=await self._duel_rivals(user_id, chat_id),
                first_name=row["first_name"],
                username=row["username"],
                chat_title=row["chat_title"],
            )

    async def find_player(self, user_id: int, chat_id: int) -> Entry | None:
        async with self._lock:
            row = await self._get_row(user_id, chat_id)
            if row is None or int(row["plays"]) <= 0:
                return None
            return _entry(row)

    async def find_player_by_username(self, chat_id: int, username: str) -> Entry | None:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT user_id, chat_id, size, username, first_name, chat_title
                FROM players
                WHERE chat_id = ? AND lower(username) = ? AND plays > 0
                LIMIT 1
                """,
                (chat_id, username.lower()),
            )
            row = await cursor.fetchone()
            return _entry(row) if row is not None else None

    async def create_duel(self, *, chat_id: int, challenger_id: int, opponent_id: int) -> int | None:
        if challenger_id == opponent_id:
            return None
        async with self._lock:
            try:
                cursor = await self.conn.execute(
                    """
                    SELECT id FROM duels
                    WHERE chat_id = ? AND status IN ('pending', 'rolling')
                      AND (
                        (challenger_id = ? AND opponent_id = ?)
                        OR (challenger_id = ? AND opponent_id = ?)
                      )
                    LIMIT 1
                    """,
                    (chat_id, challenger_id, opponent_id, opponent_id, challenger_id),
                )
                if await cursor.fetchone() is not None:
                    await self.conn.commit()
                    return None
                cursor = await self.conn.execute(
                    """
                    INSERT INTO duels (chat_id, challenger_id, opponent_id, status, created_at)
                    VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (chat_id, challenger_id, opponent_id, self._clock()),
                )
                await self.conn.commit()
                return int(cursor.lastrowid)
            except Exception:
                await self.conn.rollback()
                raise

    async def create_ffa_duel(self, *, chat_id: int, challenger_id: int) -> int | None:
        async with self._lock:
            try:
                existing = await self._pending_ffa_id(chat_id, challenger_id)
                if existing is not None:
                    await self.conn.commit()
                    return None
                cursor = await self.conn.execute(
                    """
                    INSERT INTO duels (chat_id, challenger_id, opponent_id, status, created_at)
                    VALUES (?, ?, NULL, 'pending', ?)
                    """,
                    (chat_id, challenger_id, self._clock()),
                )
                await self.conn.commit()
                return int(cursor.lastrowid)
            except Exception:
                await self.conn.rollback()
                raise

    async def find_pending_ffa(self, chat_id: int, challenger_id: int) -> Duel | None:
        async with self._lock:
            duel_id = await self._pending_ffa_id(chat_id, challenger_id)
            if duel_id is None:
                return None
            return await self._duel(duel_id)

    async def find_open_duel(self, chat_id: int, user_a: int, user_b: int) -> Duel | None:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT id FROM duels
                WHERE chat_id = ? AND status IN ('pending', 'rolling')
                  AND (
                    (challenger_id = ? AND opponent_id = ?)
                    OR (challenger_id = ? AND opponent_id = ?)
                  )
                LIMIT 1
                """,
                (chat_id, user_a, user_b, user_b, user_a),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return await self._duel(int(row["id"]))

    async def get_duel(self, duel_id: int) -> Duel | None:
        async with self._lock:
            return await self._duel(duel_id)

    async def claim_duel(self, duel_id: int, user_id: int) -> bool:
        async with self._lock:
            try:
                duel = await self._duel(duel_id)
                if duel is None or duel.status != "pending" or user_id == duel.challenger_id:
                    await self.conn.commit()
                    return False
                if duel.opponent_id is None:
                    row = await self._get_row(user_id, duel.chat_id)
                    if row is None or int(row["plays"]) <= 0:
                        await self.conn.commit()
                        return False
                    cursor = await self.conn.execute(
                        """
                        UPDATE duels
                        SET status = 'rolling', opponent_id = ?
                        WHERE id = ? AND opponent_id IS NULL AND status = 'pending'
                        """,
                        (user_id, duel_id),
                    )
                else:
                    if user_id != duel.opponent_id:
                        await self.conn.commit()
                        return False
                    cursor = await self.conn.execute(
                        """
                        UPDATE duels
                        SET status = 'rolling'
                        WHERE id = ? AND opponent_id = ? AND status = 'pending'
                        """,
                        (duel_id, user_id),
                    )
                await self.conn.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.conn.rollback()
                raise

    async def release_duel(self, duel_id: int) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE duels SET status = 'pending' WHERE id = ? AND status = 'rolling'",
                (duel_id,),
            )
            await self.conn.commit()

    async def cancel_duel(self, duel_id: int, user_id: int) -> bool:
        async with self._lock:
            try:
                cursor = await self.conn.execute(
                    """
                    UPDATE duels
                    SET status = 'cancelled'
                    WHERE id = ? AND challenger_id = ? AND status = 'pending'
                    """,
                    (duel_id, user_id),
                )
                await self.conn.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.conn.rollback()
                raise

    async def decline_duel(self, duel_id: int, user_id: int) -> bool:
        async with self._lock:
            try:
                cursor = await self.conn.execute(
                    """
                    UPDATE duels
                    SET status = 'declined'
                    WHERE id = ? AND opponent_id = ? AND status = 'pending'
                    """,
                    (duel_id, user_id),
                )
                await self.conn.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.conn.rollback()
                raise

    async def apply_duel(self, duel_id: int, challenger_roll: int, opponent_roll: int) -> DuelOutcome | None:
        if not 1 <= challenger_roll <= 6 or not 1 <= opponent_roll <= 6:
            raise ValueError("На кубике может выпасть только число от 1 до 6")
        async with self._lock:
            try:
                duel = await self._duel(duel_id)
                if duel is None or duel.status != "rolling" or duel.opponent_id is None:
                    await self.conn.commit()
                    return None
                diff = abs(challenger_roll - opponent_roll)
                challenger_delta = 0
                opponent_delta = 0
                if challenger_roll > opponent_roll:
                    challenger_delta = diff
                    opponent_delta = -diff
                elif opponent_roll > challenger_roll:
                    challenger_delta = -diff
                    opponent_delta = diff
                await self._add_size(duel.chat_id, duel.challenger_id, challenger_delta)
                await self._add_size(duel.chat_id, duel.opponent_id, opponent_delta)
                await self.conn.execute(
                    """
                    UPDATE duels
                    SET status = 'done', challenger_roll = ?, opponent_roll = ?
                    WHERE id = ? AND status = 'rolling'
                    """,
                    (challenger_roll, opponent_roll, duel_id),
                )
                if challenger_roll == opponent_roll:
                    challenger_place, opponent_place = 0, 0
                elif challenger_roll > opponent_roll:
                    challenger_place, opponent_place = 1, 2
                else:
                    challenger_place, opponent_place = 2, 1
                await self._record_contest(
                    user_id=duel.challenger_id,
                    chat_id=duel.chat_id,
                    kind="duel",
                    source_id=duel_id,
                    delta=challenger_delta,
                    place=challenger_place,
                    opponent_id=duel.opponent_id,
                )
                await self._record_contest(
                    user_id=duel.opponent_id,
                    chat_id=duel.chat_id,
                    kind="duel",
                    source_id=duel_id,
                    delta=opponent_delta,
                    place=opponent_place,
                    opponent_id=duel.challenger_id,
                )
                challenger = await self._get_row(duel.challenger_id, duel.chat_id)
                opponent = await self._get_row(duel.opponent_id, duel.chat_id)
                await self.conn.commit()
                if challenger is None or opponent is None:
                    return None
                return DuelOutcome(
                    challenger_roll=challenger_roll,
                    opponent_roll=opponent_roll,
                    diff=diff,
                    challenger_size=int(challenger["size"]),
                    opponent_size=int(opponent["size"]),
                    challenger_name=duel.challenger_name,
                    challenger_username=duel.challenger_username,
                    opponent_name=duel.opponent_name,
                    opponent_username=duel.opponent_username,
                )
            except Exception:
                await self.conn.rollback()
                raise

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
        self,
        user_id: int,
        chat_id: int | None = None,
        limit: int = 10,
    ) -> tuple[list[Entry], Standing | None]:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        user_id, chat_id, size, username, first_name, chat_title, plays,
                        ROW_NUMBER() OVER (
                            PARTITION BY user_id
                            ORDER BY
                                CASE WHEN chat_id = ? THEN 0 ELSE 1 END,
                                size DESC,
                                plays DESC,
                                chat_id
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
                (chat_id, limit),
            )
            entries = [_entry(row) for row in await cursor.fetchall()]
            standing = await self._global_standing(user_id, chat_id)
            return entries, standing

    async def chat_review(self, chat_id: int, *, week: bool) -> PeriodReview:
        since = period_start(self._clock(), self.zone, week=week)
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT
                    p.user_id,
                    COALESCE(NULLIF(pl.first_name, ''), 'Игрок') AS name,
                    p.delta,
                    p.size_after,
                    p.played_at
                FROM plays p
                LEFT JOIN players pl ON pl.user_id = p.user_id AND pl.chat_id = p.chat_id
                WHERE p.chat_id = ? AND p.played_at >= ?
                ORDER BY p.played_at, p.id
                """,
                (chat_id, since),
            )
            plays = [
                (
                    int(row["user_id"]),
                    str(row["name"]),
                    int(row["delta"]),
                    int(row["size_after"]),
                    int(row["played_at"]),
                )
                for row in await cursor.fetchall()
            ]
            cursor = await self.conn.execute(
                """
                SELECT
                    c.kind,
                    c.source_id,
                    c.user_id,
                    c.delta,
                    c.place,
                    COALESCE(NULLIF(pl.first_name, ''), 'Игрок') AS name
                FROM contest_results c
                LEFT JOIN players pl ON pl.user_id = c.user_id AND pl.chat_id = c.chat_id
                WHERE c.chat_id = ? AND c.played_at >= ?
                """,
                (chat_id, since),
            )
            contests = [
                (
                    str(row["kind"]),
                    int(row["source_id"]),
                    int(row["user_id"]),
                    str(row["name"]),
                    int(row["delta"]),
                    int(row["place"]),
                )
                for row in await cursor.fetchall()
            ]
        return _period_review(plays, contests)

    async def create_battlecup(self, *, chat_id: int, stake: int, starts_at: int) -> int | None:
        async with self._lock:
            try:
                cursor = await self.conn.execute(
                    """
                    SELECT id FROM battlecups
                    WHERE chat_id = ? AND status IN ('pending', 'running')
                    LIMIT 1
                    """,
                    (chat_id,),
                )
                if await cursor.fetchone() is not None:
                    await self.conn.commit()
                    return None
                cursor = await self.conn.execute(
                    """
                    INSERT INTO battlecups (chat_id, status, stake, starts_at, created_at)
                    VALUES (?, 'pending', ?, ?, ?)
                    """,
                    (chat_id, stake, starts_at, self._clock()),
                )
                await self.conn.commit()
                return int(cursor.lastrowid)
            except Exception:
                await self.conn.rollback()
                raise

    async def set_battlecup_message(self, battlecup_id: int, message_id: int) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE battlecups SET message_id = ? WHERE id = ?",
                (message_id, battlecup_id),
            )
            await self.conn.commit()

    async def get_battlecup(self, battlecup_id: int) -> Battlecup | None:
        async with self._lock:
            return await self._battlecup(battlecup_id)

    async def join_battlecup(self, battlecup_id: int, user_id: int) -> str:
        async with self._lock:
            try:
                cup = await self._battlecup(battlecup_id)
                if cup is None or cup.status != "pending":
                    await self.conn.commit()
                    return "closed"
                if any(player.user_id == user_id for player in cup.players):
                    await self.conn.commit()
                    return "already"
                row = await self._get_row(user_id, cup.chat_id)
                if row is None or int(row["plays"]) <= 0:
                    await self.conn.commit()
                    return "unknown"
                await self.conn.execute(
                    """
                    INSERT INTO battlecup_entries (battlecup_id, user_id, joined_at)
                    VALUES (?, ?, ?)
                    """,
                    (battlecup_id, user_id, self._clock()),
                )
                await self._add_size(cup.chat_id, user_id, -cup.stake)
                await self.conn.commit()
                return "joined"
            except Exception:
                await self.conn.rollback()
                raise

    async def requeue_running_battlecups(self, now: int) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                UPDATE battlecups
                SET status = 'pending', starts_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            await self.conn.commit()

    async def pending_battlecups(self) -> list[Battlecup]:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT id FROM battlecups
                WHERE status = 'pending'
                ORDER BY starts_at, id
                """
            )
            cups: list[Battlecup] = []
            for row in await cursor.fetchall():
                cup = await self._battlecup(int(row["id"]))
                if cup is not None:
                    cups.append(cup)
            return cups

    async def claim_due_battlecups(self, now: int) -> list[Battlecup]:
        async with self._lock:
            try:
                cursor = await self.conn.execute(
                    """
                    SELECT id FROM battlecups
                    WHERE status = 'pending' AND starts_at <= ?
                    ORDER BY starts_at, id
                    """,
                    (now,),
                )
                ids = [int(row["id"]) for row in await cursor.fetchall()]
                claimed: list[Battlecup] = []
                for battlecup_id in ids:
                    cursor = await self.conn.execute(
                        """
                        UPDATE battlecups
                        SET status = 'running'
                        WHERE id = ? AND status = 'pending'
                        """,
                        (battlecup_id,),
                    )
                    if cursor.rowcount != 1:
                        continue
                    cup = await self._battlecup(battlecup_id)
                    if cup is not None:
                        claimed.append(cup)
                await self.conn.commit()
                return claimed
            except Exception:
                await self.conn.rollback()
                raise

    async def finish_battlecup(self, battlecup_id: int, rolls: dict[int, int]) -> BattlecupOutcome | None:
        for roll in rolls.values():
            if not 0 <= roll <= 30:
                raise ValueError("Сумма кеглей должна быть от 0 до 30")
        async with self._lock:
            try:
                cup = await self._battlecup(battlecup_id)
                if cup is None or cup.status != "running":
                    await self.conn.commit()
                    return None
                player_ids = [player.user_id for player in cup.players]
                if set(rolls) != set(player_ids) or not player_ids:
                    await self.conn.rollback()
                    raise ValueError("Броски не сходятся с участниками")
                best = max(rolls.values())
                winners = [user_id for user_id, roll in rolls.items() if roll == best]
                shares = prize_shares(cup.stake * len(player_ids), winners)
                places = contest_places(rolls)
                for user_id, roll in rolls.items():
                    await self.conn.execute(
                        """
                        UPDATE battlecup_entries
                        SET roll = ?
                        WHERE battlecup_id = ? AND user_id = ?
                        """,
                        (roll, battlecup_id, user_id),
                    )
                    prize = shares.get(user_id, 0)
                    await self._add_size(cup.chat_id, user_id, prize)
                    await self._record_contest(
                        user_id=user_id,
                        chat_id=cup.chat_id,
                        kind="battlecup",
                        source_id=battlecup_id,
                        delta=prize - cup.stake,
                        place=places[user_id],
                    )
                await self.conn.execute(
                    "UPDATE battlecups SET status = 'done' WHERE id = ? AND status = 'running'",
                    (battlecup_id,),
                )
                payouts: list[BattlecupPayout] = []
                for player in cup.players:
                    row = await self._get_row(player.user_id, cup.chat_id)
                    if row is None:
                        continue
                    payouts.append(
                        BattlecupPayout(
                            user_id=player.user_id,
                            first_name=player.first_name,
                            username=player.username,
                            roll=rolls[player.user_id],
                            prize=shares.get(player.user_id, 0),
                            size=int(row["size"]),
                        )
                    )
                await self.conn.commit()
                return BattlecupOutcome(pool=cup.stake * len(player_ids), payouts=payouts)
            except Exception:
                await self.conn.rollback()
                raise

    async def refund_battlecup(self, battlecup_id: int) -> Battlecup | None:
        async with self._lock:
            try:
                cup = await self._battlecup(battlecup_id)
                if cup is None or cup.status not in {"pending", "running"}:
                    await self.conn.commit()
                    return None
                for player in cup.players:
                    await self._add_size(cup.chat_id, player.user_id, cup.stake)
                await self.conn.execute(
                    """
                    UPDATE battlecups
                    SET status = 'cancelled'
                    WHERE id = ? AND status IN ('pending', 'running')
                    """,
                    (battlecup_id,),
                )
                await self.conn.commit()
                return cup
            except Exception:
                await self.conn.rollback()
                raise

    async def _battlecup(self, battlecup_id: int) -> Battlecup | None:
        cursor = await self.conn.execute("SELECT * FROM battlecups WHERE id = ?", (battlecup_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        cursor = await self.conn.execute(
            """
            SELECT e.user_id, e.roll, p.first_name, p.username
            FROM battlecup_entries e
            JOIN players p ON p.user_id = e.user_id AND p.chat_id = ?
            WHERE e.battlecup_id = ?
            ORDER BY e.joined_at, e.user_id
            """,
            (int(row["chat_id"]), battlecup_id),
        )
        players = [
            BattlecupPlayer(
                user_id=int(item["user_id"]),
                first_name=item["first_name"],
                username=item["username"],
                roll=None if item["roll"] is None else int(item["roll"]),
            )
            for item in await cursor.fetchall()
        ]
        return Battlecup(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            status=row["status"],
            stake=int(row["stake"]),
            starts_at=int(row["starts_at"]),
            message_id=None if row["message_id"] is None else int(row["message_id"]),
            players=players,
        )

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

    async def set_clear_minutes(self, chat_id: int, minutes: int | None) -> None:
        async with self._lock:
            if minutes is None or minutes <= 0:
                await self.conn.execute("DELETE FROM chat_cleanup WHERE chat_id = ?", (chat_id,))
            else:
                await self.conn.execute(
                    """
                    INSERT INTO chat_cleanup (chat_id, minutes)
                    VALUES (?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET minutes = excluded.minutes
                    """,
                    (chat_id, minutes),
                )
            await self.conn.commit()

    async def get_clear_minutes(self, chat_id: int) -> int | None:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT minutes FROM chat_cleanup WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return int(row["minutes"])

    async def track_message(self, chat_id: int, message_id: int) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                INSERT OR IGNORE INTO bot_messages (chat_id, message_id, created_at, kind)
                VALUES (?, ?, ?, 'plain')
                """,
                (chat_id, message_id, self._clock()),
            )
            await self.conn.commit()

    async def hold_message(self, chat_id: int, message_id: int, *, kind: str, ref_id: int) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO bot_messages (chat_id, message_id, created_at, kind, ref_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    kind = excluded.kind,
                    ref_id = excluded.ref_id
                """,
                (chat_id, message_id, self._clock(), kind, ref_id),
            )
            await self.conn.commit()

    async def forget_message(self, chat_id: int, message_id: int) -> None:
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM bot_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            await self.conn.commit()

    async def due_bot_messages(self, now: int) -> list[tuple[int, int]]:
        async with self._lock:
            cursor = await self.conn.execute(
                """
                SELECT m.chat_id, m.message_id
                FROM bot_messages m
                JOIN chat_cleanup c ON c.chat_id = m.chat_id
                WHERE m.created_at + c.minutes * 60 <= ?
                  AND NOT (
                    m.kind = 'duel' AND EXISTS (
                        SELECT 1 FROM duels d
                        WHERE d.id = m.ref_id AND d.status = 'pending'
                    )
                  )
                  AND NOT (
                    m.kind = 'battlecup' AND EXISTS (
                        SELECT 1 FROM battlecups b
                        WHERE b.id = m.ref_id AND b.status = 'pending'
                    )
                  )
                """,
                (now,),
            )
            return [(int(row["chat_id"]), int(row["message_id"])) for row in await cursor.fetchall()]

    async def _pending_ffa_id(self, chat_id: int, challenger_id: int) -> int | None:
        cursor = await self.conn.execute(
            """
            SELECT id FROM duels
            WHERE chat_id = ? AND challenger_id = ? AND opponent_id IS NULL AND status = 'pending'
            LIMIT 1
            """,
            (chat_id, challenger_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["id"])

    async def _ensure_nullable_duel_opponent(self) -> None:
        cursor = await self.conn.execute("PRAGMA table_info(duels)")
        columns = {row["name"]: row for row in await cursor.fetchall()}
        opponent = columns.get("opponent_id")
        if opponent is None or int(opponent["notnull"]) == 0:
            return
        await self.conn.executescript(
            """
            CREATE TABLE duels_open (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                challenger_id INTEGER NOT NULL,
                opponent_id INTEGER,
                status TEXT NOT NULL,
                challenger_roll INTEGER,
                opponent_roll INTEGER,
                created_at INTEGER NOT NULL
            );
            INSERT INTO duels_open (
                id, chat_id, challenger_id, opponent_id, status,
                challenger_roll, opponent_roll, created_at
            )
            SELECT
                id, chat_id, challenger_id, opponent_id, status,
                challenger_roll, opponent_roll, created_at
            FROM duels;
            DROP TABLE duels;
            ALTER TABLE duels_open RENAME TO duels;
            """
        )

    async def _ensure_contest_opponent(self) -> None:
        cursor = await self.conn.execute("PRAGMA table_info(contest_results)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "opponent_id" not in columns:
            await self.conn.execute("ALTER TABLE contest_results ADD COLUMN opponent_id INTEGER")

    async def _duel_rivals(self, user_id: int, chat_id: int) -> list[tuple[str, int, int, int, int]]:
        cursor = await self.conn.execute(
            """
            SELECT
                c.opponent_id,
                MAX(COALESCE(NULLIF(p.first_name, ''), 'Игрок')) AS name,
                SUM(CASE WHEN c.place = 1 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN c.place = 2 THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN c.place = 0 THEN 1 ELSE 0 END) AS ties,
                SUM(c.delta) AS net
            FROM contest_results c
            LEFT JOIN players p
                ON p.user_id = c.opponent_id AND p.chat_id = c.chat_id
            WHERE c.user_id = ? AND c.chat_id = ? AND c.kind = 'duel' AND c.opponent_id IS NOT NULL
            GROUP BY c.opponent_id
            ORDER BY COUNT(*) DESC, wins DESC, c.opponent_id
            """,
            (user_id, chat_id),
        )
        return [
            (row["name"], int(row["wins"]), int(row["losses"]), int(row["ties"]), int(row["net"]))
            for row in await cursor.fetchall()
        ]

    async def _contest_rows(self, user_id: int, chat_id: int) -> list[tuple[str, int, int]]:
        cursor = await self.conn.execute(
            """
            SELECT kind, delta, place FROM contest_results
            WHERE user_id = ? AND chat_id = ?
            ORDER BY played_at, id
            """,
            (user_id, chat_id),
        )
        return [(row["kind"], int(row["delta"]), int(row["place"])) for row in await cursor.fetchall()]

    async def _record_contest(
        self,
        *,
        user_id: int,
        chat_id: int,
        kind: str,
        source_id: int,
        delta: int,
        place: int,
        played_at: int | None = None,
        opponent_id: int | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO contest_results (
                user_id, chat_id, kind, source_id, delta, place, played_at, opponent_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                chat_id,
                kind,
                source_id,
                delta,
                place,
                played_at or self._clock(),
                opponent_id,
            ),
        )

    async def _backfill_contests(self) -> None:
        cursor = await self.conn.execute(
            """
            SELECT id, chat_id, challenger_id, opponent_id, challenger_roll, opponent_roll, created_at
            FROM duels
            WHERE status = 'done' AND challenger_roll IS NOT NULL AND opponent_roll IS NOT NULL
            """
        )
        for row in await cursor.fetchall():
            challenger_roll = int(row["challenger_roll"])
            opponent_roll = int(row["opponent_roll"])
            diff = abs(challenger_roll - opponent_roll)
            if challenger_roll == opponent_roll:
                challenger_delta, opponent_delta = 0, 0
                challenger_place, opponent_place = 0, 0
            elif challenger_roll > opponent_roll:
                challenger_delta, opponent_delta = diff, -diff
                challenger_place, opponent_place = 1, 2
            else:
                challenger_delta, opponent_delta = -diff, diff
                challenger_place, opponent_place = 2, 1
            played_at = int(row["created_at"])
            await self._record_contest(
                user_id=int(row["challenger_id"]),
                chat_id=int(row["chat_id"]),
                kind="duel",
                source_id=int(row["id"]),
                delta=challenger_delta,
                place=challenger_place,
                played_at=played_at,
                opponent_id=int(row["opponent_id"]),
            )
            await self._record_contest(
                user_id=int(row["opponent_id"]),
                chat_id=int(row["chat_id"]),
                kind="duel",
                source_id=int(row["id"]),
                delta=opponent_delta,
                place=opponent_place,
                played_at=played_at,
                opponent_id=int(row["challenger_id"]),
            )
        await self.conn.execute(
            """
            UPDATE contest_results
            SET opponent_id = (
                SELECT CASE
                    WHEN contest_results.user_id = duels.challenger_id THEN duels.opponent_id
                    ELSE duels.challenger_id
                END
                FROM duels
                WHERE duels.id = contest_results.source_id
            )
            WHERE kind = 'duel' AND opponent_id IS NULL
            """
        )
        cursor = await self.conn.execute(
            """
            SELECT id, chat_id, stake, created_at FROM battlecups WHERE status = 'done'
            """
        )
        cups = await cursor.fetchall()
        for cup in cups:
            entries = await self.conn.execute(
                "SELECT user_id, roll FROM battlecup_entries WHERE battlecup_id = ?",
                (int(cup["id"]),),
            )
            rows = await entries.fetchall()
            if not rows or any(item["roll"] is None for item in rows):
                continue
            rolls = {int(item["user_id"]): int(item["roll"]) for item in rows}
            best = max(rolls.values())
            winners = [user_id for user_id, roll in rolls.items() if roll == best]
            shares = prize_shares(int(cup["stake"]) * len(rolls), winners)
            places = contest_places(rolls)
            for user_id, _roll in rolls.items():
                await self._record_contest(
                    user_id=user_id,
                    chat_id=int(cup["chat_id"]),
                    kind="battlecup",
                    source_id=int(cup["id"]),
                    delta=shares.get(user_id, 0) - int(cup["stake"]),
                    place=places[user_id],
                    played_at=int(cup["created_at"]),
                )

    async def _last_played_at(self, user_id: int, chat_id: int) -> int | None:
        cursor = await self.conn.execute(
            """
            SELECT played_at FROM plays
            WHERE user_id = ? AND chat_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, chat_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["played_at"])

    async def _add_size(self, chat_id: int, user_id: int, delta: int) -> None:
        if delta == 0:
            return
        await self.conn.execute(
            "UPDATE players SET size = size + ? WHERE user_id = ? AND chat_id = ?",
            (delta, user_id, chat_id),
        )

    async def _duel(self, duel_id: int) -> Duel | None:
        cursor = await self.conn.execute(
            """
            SELECT
                d.id, d.chat_id, d.challenger_id, d.opponent_id, d.status,
                c.first_name AS challenger_name, c.username AS challenger_username,
                o.first_name AS opponent_name, o.username AS opponent_username
            FROM duels d
            JOIN players c ON c.user_id = d.challenger_id AND c.chat_id = d.chat_id
            LEFT JOIN players o ON o.user_id = d.opponent_id AND o.chat_id = d.chat_id
            WHERE d.id = ?
            """,
            (duel_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Duel(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            challenger_id=int(row["challenger_id"]),
            opponent_id=None if row["opponent_id"] is None else int(row["opponent_id"]),
            status=row["status"],
            challenger_name=row["challenger_name"],
            challenger_username=row["challenger_username"],
            opponent_name=row["opponent_name"] or "",
            opponent_username=row["opponent_username"],
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

    async def _global_standing(self, user_id: int, chat_id: int | None) -> Standing | None:
        cursor = await self.conn.execute(
            """
            WITH chosen AS (
                SELECT
                    user_id,
                    size,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id
                        ORDER BY
                            CASE WHEN chat_id = ? THEN 0 ELSE 1 END,
                            size DESC,
                            plays DESC,
                            chat_id
                    ) AS rn
                FROM players
                WHERE plays > 0
            ),
            best AS (
                SELECT user_id, size FROM chosen WHERE rn = 1
            )
            SELECT
                (SELECT size FROM best WHERE user_id = ?) AS size,
                (
                    SELECT COUNT(*) + 1 FROM best
                    WHERE size > (SELECT size FROM best WHERE user_id = ?)
                ) AS rank,
                (SELECT COUNT(*) FROM best) AS total
            """,
            (chat_id, user_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None or row["size"] is None:
            return None
        return Standing(rank=int(row["rank"]), total=int(row["total"]), size=int(row["size"]))


def play_streaks(deltas: list[int]) -> tuple[int, int]:
    best = 0
    worst = 0
    run = 0
    sign = 0
    for delta in deltas:
        if delta > 0:
            run = delta if sign != 1 else run + delta
            sign = 1
            best = max(best, run)
        elif delta < 0:
            run = delta if sign != -1 else run + delta
            sign = -1
            worst = min(worst, run)
        else:
            run = 0
            sign = 0
    return best, worst


def _period_review(
    plays: list[tuple[int, str, int, int, int]],
    contests: list[tuple[str, int, int, str, int, int]],
) -> PeriodReview:
    totals: dict[int, int] = {}
    names: dict[int, str] = {}
    for user_id, name, delta, _size_after, _played_at in plays:
        totals[user_id] = totals.get(user_id, 0) + delta
        names[user_id] = name
    growth = [(user_id, names[user_id], total) for user_id, total in totals.items()]
    measures = [(user_id, name, delta) for user_id, name, delta, _size_after, _played_at in plays]

    duel_rows = [row for row in contests if row[0] == "duel"]
    cup_rows = [row for row in contests if row[0] == "battlecup"]
    duel_ids = {row[1] for row in duel_rows}
    cup_ids = {row[1] for row in cup_rows}
    wins: dict[int, int] = {}
    cup_wins: dict[int, int] = {}
    cup_nets: dict[int, int] = {}
    for _kind, _source_id, user_id, name, delta, place in duel_rows:
        names[user_id] = name
        if place == 1:
            wins[user_id] = wins.get(user_id, 0) + 1
    for _kind, _source_id, user_id, name, delta, place in cup_rows:
        names[user_id] = name
        cup_nets[user_id] = cup_nets.get(user_id, 0) + delta
        if place == 1:
            cup_wins[user_id] = cup_wins.get(user_id, 0) + 1

    return PeriodReview(
        plays=len(plays),
        play_sum=sum(delta for _user_id, _name, delta, _size_after, _played_at in plays),
        best_growth=_highest(growth),
        worst_growth=_lowest(growth),
        best_measure=_highest(measures),
        worst_measure=_lowest(measures),
        duels=len(duel_ids),
        duel_leader=_highest([(user_id, names[user_id], count) for user_id, count in wins.items()]),
        duel_haul=_duel_haul(duel_rows),
        cups=len(cup_ids),
        cup_leader=_highest([(user_id, names[user_id], count) for user_id, count in cup_wins.items()]),
        cup_sponsor=_lowest([(user_id, names[user_id], total) for user_id, total in cup_nets.items()]),
        measures=tuple(
            Measure(name, delta, size_after, played_at)
            for _user_id, name, delta, size_after, played_at in plays
        ),
    )


def _highest(rows: list[tuple[int, str, int]]) -> NamedValue | None:
    positive = [row for row in rows if row[2] > 0]
    if not positive:
        return None
    user_id, name, value = max(positive, key=lambda row: (row[2], -row[0]))
    return NamedValue(name, value)


def _lowest(rows: list[tuple[int, str, int]]) -> NamedValue | None:
    negative = [row for row in rows if row[2] < 0]
    if not negative:
        return None
    _user_id, name, value = min(negative, key=lambda row: (row[2], row[0]))
    return NamedValue(name, value)


def _duel_haul(rows: list[tuple[str, int, int, str, int, int]]) -> DuelHaul | None:
    by_source: dict[int, list[tuple[str, int, int]]] = {}
    for _kind, source_id, _user_id, name, delta, place in rows:
        by_source.setdefault(source_id, []).append((name, delta, place))
    best: DuelHaul | None = None
    for participants in by_source.values():
        winner = next((item for item in participants if item[2] == 1 and item[1] > 0), None)
        loser = next((item for item in participants if item[2] == 2), None)
        if winner is None or loser is None:
            continue
        haul = DuelHaul(winner[0], loser[0], winner[1])
        if best is None or haul.amount > best.amount:
            best = haul
    return best


def contest_places(rolls: dict[int, int]) -> dict[int, int]:
    return {user_id: 1 + sum(other > roll for other in rolls.values()) for user_id, roll in rolls.items()}


def _contest_summary(rows: list[tuple[str, int, int]]) -> tuple[int, int, int, int, int]:
    duel_wins = duel_losses = duel_ties = cup_wins = cup_played = 0
    for kind, _delta, place in rows:
        if kind == "duel":
            if place == 1:
                duel_wins += 1
            elif place == 2:
                duel_losses += 1
            else:
                duel_ties += 1
        elif kind == "battlecup":
            cup_played += 1
            if place == 1:
                cup_wins += 1
    return duel_wins, duel_losses, duel_ties, cup_wins, cup_played


def prize_shares(pool: int, winner_ids: list[int]) -> dict[int, int]:
    if pool < 0 or not winner_ids:
        raise ValueError("Нечего делить")
    ordered = sorted(winner_ids)
    share, extra = divmod(pool, len(ordered))
    return {user_id: share + (1 if index < extra else 0) for index, user_id in enumerate(ordered)}


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
