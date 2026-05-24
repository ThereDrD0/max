from __future__ import annotations

import re
import unicodedata
from datetime import datetime


MAX_START_PAYLOAD_LIMIT = 128
EVENT_PAYLOAD_PREFIX = "e_"
_MAX_SLUG_LENGTH = MAX_START_PAYLOAD_LIMIT - len(EVENT_PAYLOAD_PREFIX)
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_SLUG_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_CYRILLIC_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def parse_start_payload(raw: str | None) -> str | None:
    if not raw or len(raw) > MAX_START_PAYLOAD_LIMIT:
        return None
    if not raw.startswith(EVENT_PAYLOAD_PREFIX):
        return None
    slug = raw[len(EVENT_PAYLOAD_PREFIX) :]
    if not _is_valid_slug(slug):
        return None
    return slug


def build_start_payload(slug: str) -> str:
    if not _is_valid_slug(slug):
        raise ValueError("Некорректный slug мероприятия")
    payload = f"{EVENT_PAYLOAD_PREFIX}{slug}"
    if len(payload) > MAX_START_PAYLOAD_LIMIT:
        raise ValueError("Payload диплинка MAX длиннее 128 символов")
    return payload


def build_event_deeplink(bot_username: str, slug: str) -> str | None:
    username = (bot_username or "").strip().removeprefix("@")
    if not username:
        return None
    return f"https://max.ru/{username}?start={build_start_payload(slug)}"


def build_default_event_slug(title: str, starts_at: datetime) -> str:
    date_part = starts_at.date().isoformat()
    base = _slug_base(title) or "event"
    max_base_length = max(1, _MAX_SLUG_LENGTH - len(date_part) - 1)
    base = base[:max_base_length].strip("-") or "event"
    return f"{base}-{date_part}"


def _is_valid_slug(slug: str | None) -> bool:
    return bool(slug and _SLUG_RE.fullmatch(slug))


def _slug_base(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).lower()
    chars: list[str] = []
    for char in normalized:
        if unicodedata.category(char) == "Mn":
            continue
        if char in _CYRILLIC_TRANSLIT:
            chars.append(_CYRILLIC_TRANSLIT[char])
        elif char.isascii():
            chars.append(char)
        else:
            chars.append("-")
    return _SLUG_SEPARATOR_RE.sub("-", "".join(chars)).strip("-")
