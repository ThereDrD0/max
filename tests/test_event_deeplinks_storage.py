from __future__ import annotations

import pytest

from app.domain import DuplicateEventSlugError
from app.seed import load_seed_events
from tests.conftest import create_event


def test_event_slug_roundtrip(storage, fixed_now):
    event = create_event(storage, fixed_now, title="День открытых дверей")

    storage.assign_event_slug(event.id, "it-open-day-2026-06-15", now=fixed_now)

    assert storage.get_event_by_slug("it-open-day-2026-06-15").id == event.id
    assert storage.get_event_slug(event.id) == "it-open-day-2026-06-15"


def test_assigning_same_slug_to_same_event_is_idempotent(storage, fixed_now):
    event = create_event(storage, fixed_now)

    storage.assign_event_slug(event.id, "it-open-day-2026-06-15", now=fixed_now)
    storage.assign_event_slug(event.id, "it-open-day-2026-06-15", now=fixed_now)

    assert storage.get_event_by_slug("it-open-day-2026-06-15").id == event.id


def test_assigning_same_slug_to_other_event_raises(storage, fixed_now):
    first = create_event(storage, fixed_now, title="Первое мероприятие")
    second = create_event(storage, fixed_now, title="Второе мероприятие")
    storage.assign_event_slug(first.id, "shared-slug", now=fixed_now)

    with pytest.raises(DuplicateEventSlugError):
        storage.assign_event_slug(second.id, "shared-slug", now=fixed_now)


def test_seed_assigns_slug_to_new_and_existing_events(tmp_path, storage, fixed_now):
    seed_file = tmp_path / "events.yaml"
    seed_file.write_text(
        """
events:
  - slug: "it-open-day-2026-06-15"
    title: "День открытых дверей"
    description: "Описание"
    requirements: "Код записи"
    starts_at: "2026-06-15T10:00:00+03:00"
    duration_minutes: 60
    format: "online"
    location_or_url: "https://example.edu/live"
    cancellation_policy_text: "Отмена до начала"
    capacity_total: 10
    late_cancel_policy: "deny"
""",
        encoding="utf-8",
    )

    created = load_seed_events(storage, seed_file)
    created_again = load_seed_events(storage, seed_file)

    event = storage.get_event_by_slug("it-open-day-2026-06-15")
    assert created == 1
    assert created_again == 0
    assert event is not None
    assert event.title == "День открытых дверей"
