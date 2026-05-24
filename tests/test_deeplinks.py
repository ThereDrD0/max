from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.bot.deeplinks import (
    MAX_START_PAYLOAD_LIMIT,
    build_default_event_slug,
    build_event_deeplink,
    build_start_payload,
    parse_start_payload,
)


def test_parse_event_start_payload_returns_slug():
    assert parse_start_payload("e_it-open-day-2026-06-15") == "it-open-day-2026-06-15"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "it-open-day-2026-06-15",
        "x_it-open-day-2026-06-15",
        "e_",
        "e_IT-open-day",
        "e_it open day",
        "e_день-открытых-дверей",
        "e_it/open-day",
        "e_" + "a" * (MAX_START_PAYLOAD_LIMIT - 1),
    ],
)
def test_parse_event_start_payload_rejects_invalid_values(payload):
    assert parse_start_payload(payload) is None


def test_build_start_payload_keeps_payload_inside_max_limit():
    payload = build_start_payload("it-open-day-2026-06-15")

    assert payload == "e_it-open-day-2026-06-15"
    assert len(payload) <= MAX_START_PAYLOAD_LIMIT


def test_build_start_payload_rejects_invalid_slug():
    with pytest.raises(ValueError):
        build_start_payload("День открытых дверей")


def test_build_event_deeplink_uses_bot_username_and_slug():
    assert (
        build_event_deeplink("id123_bot", "it-open-day-2026-06-15")
        == "https://max.ru/id123_bot?start=e_it-open-day-2026-06-15"
    )


def test_build_event_deeplink_returns_none_without_username():
    assert build_event_deeplink("", "it-open-day-2026-06-15") is None


def test_build_default_event_slug_is_valid_for_cyrillic_title():
    slug = build_default_event_slug(
        "День открытых дверей ИТ-института",
        datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc),
    )

    assert slug.endswith("-2026-06-15")
    assert parse_start_payload(f"e_{slug}") == slug
    assert len(build_start_payload(slug)) <= MAX_START_PAYLOAD_LIMIT
