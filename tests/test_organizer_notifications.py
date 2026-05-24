from __future__ import annotations

import pytest

from app.domain import AccessDeniedError, InvalidNotificationKindError
from app.enums import NotificationKind, RegistrationStatus
from app.services.organizer import OrganizerService
from app.services.registration import RegistrationService
from tests.conftest import create_event


def seed_event_with_registration(storage, fixed_now):
    event = create_event(storage, fixed_now, title="Экскурсия по кампусу", capacity=5)
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    registration_service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "FINDME",
    )
    registration_service.upsert_user(101, "Анна")
    registration_service.record_profile_consent(101, "hackathon-2026-05")
    registration = registration_service.create_registration(101, event.id, None)
    return event, registration


def test_organizer_can_only_see_managed_events(storage, fixed_now):
    event, _ = seed_event_with_registration(storage, fixed_now)
    service = OrganizerService(storage, now=lambda: fixed_now)

    assert [item.id for item in service.list_events(501)] == [event.id]
    with pytest.raises(AccessDeniedError):
        service.get_event_registrations(777, event.id)


def test_organizer_searches_registration_by_code_and_updates_status(
    storage, fixed_now
):
    event, registration = seed_event_with_registration(storage, fixed_now)
    service = OrganizerService(storage, now=lambda: fixed_now)

    found = service.find_registration_by_code(501, event.id, "FINDME")
    service.mark_attended(501, found.id)

    assert found.id == registration.id
    assert found.status == RegistrationStatus.ATTENDED


def test_organizer_can_close_registration(storage, fixed_now):
    event, _ = seed_event_with_registration(storage, fixed_now)
    service = OrganizerService(storage, now=lambda: fixed_now)

    closed = service.close_registration(501, event.id)

    assert closed.registration_closed is True
    assert storage.get_event(event.id).registration_closed is True


def test_manual_notifications_are_limited_to_event_templates(
    storage, fixed_now
):
    event, _ = seed_event_with_registration(storage, fixed_now)
    service = OrganizerService(storage, now=lambda: fixed_now)

    service.enqueue_manual_notification(
        501,
        event.id,
        NotificationKind.VENUE_CHANGED,
    )

    item = [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.VENUE_CHANGED
    ][0]
    assert item.event_id == event.id
    assert "аудитория" in item.message_text.lower()

    with pytest.raises(InvalidNotificationKindError):
        service.enqueue_manual_notification(
            501,
            event.id,
            NotificationKind.REMINDER_1H,
        )
