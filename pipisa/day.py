from __future__ import annotations

from datetime import datetime, timedelta, tzinfo


def local_date(timestamp: int, zone: tzinfo):
    return datetime.fromtimestamp(timestamp, zone).date()


def next_midnight(timestamp: int, zone: tzinfo) -> int:
    current = datetime.fromtimestamp(timestamp, zone)
    following = current.date() + timedelta(days=1)
    midnight = datetime(following.year, following.month, following.day, tzinfo=zone)
    return int(midnight.timestamp())


def can_play(now: int, last_played: int | None, zone: tzinfo) -> bool:
    if last_played is None:
        return True
    return local_date(last_played, zone) < local_date(now, zone)


def seconds_until_next_day(now: int, zone: tzinfo) -> int:
    return max(0, next_midnight(now, zone) - now)
