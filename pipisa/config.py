from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: Path
    grow_min: int
    grow_max: int
    zone: tzinfo

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv(ROOT / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "Нет BOT_TOKEN. Скопируй .env.example в .env и вставь токен от @BotFather."
            )
        if ":" not in token:
            raise SystemExit("BOT_TOKEN выглядит неправильно. Скопируй его целиком у @BotFather.")

        db_raw = os.getenv("DB_PATH", "data/pipisa.db").strip() or "data/pipisa.db"
        db_path = Path(db_raw)
        if not db_path.is_absolute():
            db_path = ROOT / db_path

        grow_min = _env_int("GROW_MIN", -5)
        grow_max = _env_int("GROW_MAX", 10)
        zone_name = os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"

        if grow_min > grow_max:
            raise SystemExit("GROW_MIN не может быть больше GROW_MAX.")
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError as exc:
            raise SystemExit(f"Неизвестный часовой пояс TIMEZONE={zone_name}.") from exc

        return cls(
            bot_token=token,
            db_path=db_path,
            grow_min=grow_min,
            grow_max=grow_max,
            zone=zone,
        )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} должно быть целым числом.") from exc
