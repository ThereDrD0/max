from __future__ import annotations

from datetime import date, time

from app.bot.organizer_datetime import parse_organizer_date, parse_organizer_time


def test_parse_organizer_date_accepts_numeric_formats() -> None:
    today = date(2026, 5, 21)

    assert parse_organizer_date("12.03.2026", today=today) == date(2026, 3, 12)
    assert parse_organizer_date("12:03:2026", today=today) == date(2026, 3, 12)
    assert parse_organizer_date("12/03/2026", today=today) == date(2026, 3, 12)
    assert parse_organizer_date("12.03", today=today) == date(2026, 3, 12)
    assert parse_organizer_date("12", today=today) == date(2026, 5, 12)


def test_parse_organizer_date_accepts_russian_months() -> None:
    today = date(2026, 5, 21)

    assert parse_organizer_date("12 марта", today=today) == date(2026, 3, 12)
    assert parse_organizer_date("12 марта 2027", today=today) == date(2027, 3, 12)


def test_parse_organizer_time_accepts_text_and_clock_formats() -> None:
    assert parse_organizer_time("12") == time(12, 0)
    assert parse_organizer_time("12 часов") == time(12, 0)
    assert parse_organizer_time("09:30") == time(9, 30)
