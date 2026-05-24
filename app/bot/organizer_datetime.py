from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


MOSCOW_TZ = ZoneInfo("Europe/Moscow")

_MONTHS = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}


def parse_organizer_date(raw: str, *, today: date | None = None) -> date:
    current = today or datetime.now(MOSCOW_TZ).date()
    text = " ".join((raw or "").strip().lower().replace("ё", "е").split())
    if not text:
        raise ValueError("Дата не указана")

    text_match = re.fullmatch(r"(\d{1,2})\s+([а-яе]+)(?:\s+(\d{4}))?", text)
    if text_match:
        day = int(text_match.group(1))
        month_name = text_match.group(2)
        month = _MONTHS.get(month_name)
        if month is None:
            raise ValueError("Неизвестный месяц")
        year = int(text_match.group(3) or current.year)
        return date(year, month, day)

    parts = [
        item
        for item in re.split(r"[\.:\-/\s]+", text)
        if item
    ]
    if not 1 <= len(parts) <= 3 or not all(item.isdigit() for item in parts):
        raise ValueError("Не удалось разобрать дату")

    day = int(parts[0])
    month = int(parts[1]) if len(parts) >= 2 else current.month
    year = int(parts[2]) if len(parts) == 3 else current.year
    if year < 100:
        year += 2000
    return date(year, month, day)


def parse_organizer_time(raw: str) -> time:
    text = " ".join((raw or "").strip().lower().replace("ё", "е").split())
    match = re.fullmatch(r"(\d{1,2})(?:[:\.](\d{1,2}))?(?:\s*(?:час|часа|часов|ч))?", text)
    if match is None:
        raise ValueError("Не удалось разобрать время")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    return time(hour, minute)


def combine_moscow_datetime(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=MOSCOW_TZ).astimezone(ZoneInfo("UTC"))
