from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = re.compile(r"(-?\d+)\s*(?:см)?\s*$", re.IGNORECASE)
_LEADING_PLACE = re.compile(r"^\d+[\.\)]\s*")
_USERNAME = re.compile(r"@([A-Za-z0-9_]{4,})")
_PAREN_USERNAME = re.compile(r"\s*\(@[A-Za-z0-9_]+\)")

MIN_SIZE = -1_000_000
MAX_SIZE = 10_000_000


@dataclass(frozen=True)
class ImportItem:
    first_name: str
    username: str | None
    size: int


def parse_size(text: str) -> int | None:
    cleaned = text.strip().replace("−", "-").replace("—", "-").replace("–", "-")
    match = re.fullmatch(r"(-?\d+)\s*(?:см)?", cleaned, re.IGNORECASE)
    if match is None:
        return None
    size = int(match.group(1))
    if size < MIN_SIZE or size > MAX_SIZE:
        return None
    return size


def command_body(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    parts = lines[0].split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    return "\n".join([rest, *lines[1:]]).strip()


def parse_import_text(text: str) -> tuple[list[ImportItem], list[str]]:
    items: list[ImportItem] = []
    broken: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("/"):
            continue
        if not re.search(r"\d", line):
            continue
        item = _parse_line(line)
        if item is None:
            broken.append(line)
            continue
        key = (item.username or "", item.first_name.casefold())
        if key in seen:
            items = [existing for existing in items if (existing.username or "", existing.first_name.casefold()) != key]
        seen.add(key)
        items.append(item)
    return items, broken


def _parse_line(line: str) -> ImportItem | None:
    text = _LEADING_PLACE.sub("", line.strip())
    text = text.replace("−", "-").replace("—", "-").replace("–", "-")
    match = _NUMBER.search(text)
    if match is None:
        return None
    size = int(match.group(1))
    if size < MIN_SIZE or size > MAX_SIZE:
        return None
    name = text[: match.start()].strip(" \t-:")
    if not name:
        return None
    username_match = _USERNAME.search(name)
    username = username_match.group(1).lower() if username_match else None
    display = _PAREN_USERNAME.sub("", name)
    display = _USERNAME.sub("", display).strip()
    if not display:
        display = username or name
    return ImportItem(first_name=display, username=username, size=size)
