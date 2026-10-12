from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: Path
    grow_min: int
    grow_max: int
    cooldown_seconds: int

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
        cooldown_hours = _env_int("COOLDOWN_HOURS", 24)

        if grow_min > grow_max:
            raise SystemExit("GROW_MIN не может быть больше GROW_MAX.")
        if cooldown_hours <= 0:
            raise SystemExit("COOLDOWN_HOURS должен быть больше нуля.")

        return cls(
            bot_token=token,
            db_path=db_path,
            grow_min=grow_min,
            grow_max=grow_max,
            cooldown_seconds=cooldown_hours * 3600,
        )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} должно быть целым числом.") from exc
