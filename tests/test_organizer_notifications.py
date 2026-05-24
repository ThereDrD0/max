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


def test_organizer_mark_attended_enqueues_notification_when_enabled(
    storage,
    fixed_now,
):
    event, registration = seed_event_with_registration(storage, fixed_now)
    service = OrganizerService(storage, now=lambda: fixed_now)

    updated = service.mark_attended_with_notification(501, registration.id)

    assert updated.status == RegistrationStatus.ATTENDED
    notifications = [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.ATTENDANCE_MARKED
    ]
    assert len(notifications) == 1
    assert notifications[0].event_id == event.id
    assert notifications[0].registration_id == registration.id
    assert notifications[0].user_id == registration.user_id
    assert "Организатор отметил, что вы пришли" in notifications[0].message_text


def test_organizer_mark_attended_skips_notification_when_disabled(
    storage,
    fixed_now,
):
    _, registration = seed_event_with_registration(storage, fixed_now)
    registration_service = RegistrationService(storage, now=lambda: fixed_now)
    registration_service.set_notifications_enabled(
        registration.user_id,
        registration.id,
        enabled=False,
    )
    service = OrganizerService(storage, now=lambda: fixed_now)

    service.mark_attended_with_notification(501, registration.id)

    assert [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.ATTENDANCE_MARKED
    ] == []


def test_organizer_can_close_registration(storage, fixed_now):
    event, _ = seed_event_with_registration(storage, fixed_now)
    service = OrganizerService(storage, now=lambda: fixed_now)

    closed = service.close_registration(501, event.id)

    assert closed.registration_closed is True
    assert storage.get_event(event.id).registration_closed is True


def test_organizer_can_close_event_and_enqueue_notifications_for_all_participants(
    storage,
    fixed_now,
):
    event, first = seed_event_with_registration(storage, fixed_now)
    registration_service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "FIND02",
    )
    registration_service.upsert_user(102, "Борис")
    registration_service.record_profile_consent(102, "hackathon-2026-05")
    second = registration_service.create_registration(102, event.id, None)
    registration_service.set_notifications_enabled(102, second.id, enabled=False)
    service = OrganizerService(storage, now=lambda: fixed_now)

    result = service.close_event(501, event.id)

    assert result.event.registration_closed is True
    assert result.notification_count == 2
    assert storage.get_registration(first.id).status == RegistrationStatus.CANCELED_BY_ORGANIZER
    assert storage.get_registration(second.id).status == RegistrationStatus.CANCELED_BY_ORGANIZER
    notifications = [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.EVENT_CLOSED
    ]
    assert [item.user_id for item in notifications] == [101, 102]
    assert all(item.registration_id is None for item in notifications)
    assert all("Мероприятие «Экскурсия по кампусу» закрыто" in item.message_text for item in notifications)


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
    with pytest.raises(InvalidNotificationKindError):
        service.enqueue_manual_notification(
            501,
            event.id,
            NotificationKind.EVENT_CLOSED,
        )


def test_organizer_manual_reminder_can_target_single_slot_with_custom_text(
    storage, fixed_now
):
    event = create_event(
        storage,
        fixed_now,
        title="Экскурсия по лабораториям",
        with_slots=True,
    )
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    registration_service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "SLOT01",
    )
    registration_service.upsert_user(101, "Анна")
    registration_service.record_profile_consent(101, "hackathon-2026-05")
    first = registration_service.create_registration(
        101,
        event.id,
        event.slots[0].id,
    )
    registration_service.code_generator = lambda: "SLOT02"
    registration_service.upsert_user(102, "Петр")
    registration_service.record_profile_consent(102, "hackathon-2026-05")
    registration_service.create_registration(102, event.id, event.slots[1].id)
    service = OrganizerService(storage, now=lambda: fixed_now)

    created = service.enqueue_manual_reminder(
        501,
        event.id,
        slot_id=event.slots[0].id,
        custom_text="Возьмите с собой студенческий билет.",
    )

    assert [item.user_id for item in created] == [101]
    assert created[0].kind == NotificationKind.MANUAL_REMINDER
    assert created[0].registration_id == first.id
    assert "🔔 Напоминание о мероприятии" in created[0].message_text
    assert "📅 Начало: 24.05.2026 12:00 (через 3 дня)" in created[0].message_text
    assert "Возьмите с собой студенческий билет." in created[0].message_text
    assert "🎫 Код записи: SLOT01" in created[0].message_text
    assert "🕒 Слот: 10:00" in created[0].message_text


def test_organizer_manual_reminder_uses_auto_text_when_custom_text_is_blank(
    storage, fixed_now
):
    event, registration = seed_event_with_registration(storage, fixed_now)
    service = OrganizerService(storage, now=lambda: fixed_now)

    created = service.enqueue_manual_reminder(
        501,
        event.id,
        slot_id=None,
        custom_text="   ",
    )

    assert len(created) == 1
    assert created[0].user_id == registration.user_id
    assert "скоро начнётся" not in created[0].message_text
    assert "🔔 Напоминание о мероприятии" in created[0].message_text
    assert "📅 Начало: 24.05.2026 12:00 (через 3 дня)" in created[0].message_text
    assert event.title in created[0].message_text
    assert f"🎫 Код записи: {registration.code}" in created[0].message_text
