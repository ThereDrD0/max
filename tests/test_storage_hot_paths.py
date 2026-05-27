from __future__ import annotations

from datetime import timedelta

from app.enums import EventFormat
from app.services.registration import RegistrationService
from app.storage.entities import Event, OrganizerState, Registration
from app.storage.ydb import YdbStorage
from tests.conftest import create_event


def test_touch_user_creates_updates_and_keeps_name_for_empty_touch(storage, fixed_now):
    storage.touch_user(101, "Анна", now=fixed_now)

    created = storage.get_user(101)
    assert created is not None
    assert created.display_name == "Анна"
    assert created.created_at == fixed_now

    later = fixed_now + timedelta(minutes=5)
    storage.touch_user(101, "Анна Новая", now=later)

    updated = storage.get_user(101)
    assert updated is not None
    assert updated.display_name == "Анна Новая"
    assert updated.created_at == fixed_now
    assert updated.updated_at == later

    storage.touch_user(101, "", now=later + timedelta(minutes=5))

    unchanged = storage.get_user(101)
    assert unchanged is not None
    assert unchanged.display_name == "Анна Новая"


def test_ydb_touch_user_uses_supported_conditional_insert_shape(fixed_now, monkeypatch):
    storage = object.__new__(YdbStorage)
    calls = []

    monkeypatch.setattr(storage, "_execute", lambda query, params: calls.append((query, params)))

    storage.touch_user(101, "Анна", now=fixed_now)

    assert len(calls) == 1
    query, _ = calls[0]
    insert_part = query.split("INSERT INTO users", 1)[1]
    assert "WHERE NOT EXISTS" in insert_part
    assert "FROM (" in insert_part
    assert "new_user.updated_at AS created_at" in insert_part
    assert "new_user.updated_at AS updated_at" in insert_part


def test_ydb_create_registration_returns_transaction_result_without_reload(fixed_now, monkeypatch):
    storage = object.__new__(YdbStorage)
    event = Event(
        id=10,
        title="День открытых дверей",
        description="",
        requirements="",
        starts_at=fixed_now + timedelta(days=1),
        duration_minutes=60,
        format=EventFormat.IN_PERSON,
        location_or_url="Аудитория 1",
        cancellation_policy_text="",
        capacity_total=20,
    )
    registration = Registration(
        id=20,
        code="ABC123",
        user_id=101,
        event_id=event.id,
        slot_id=None,
        created_at=fixed_now,
        updated_at=fixed_now,
        event=event,
    )

    class PoolStub:
        def retry_operation_sync(self, callee, retry_settings=None):
            return registration

    storage.pool = PoolStub()

    def fail_get_registration(*args, **kwargs):
        raise AssertionError("create_registration не должен перечитывать созданную запись")

    monkeypatch.setattr(storage, "get_registration", fail_get_registration)

    returned = storage.create_registration(
        user_id=101,
        event_id=event.id,
        slot_id=None,
        now=fixed_now,
        code_generator=lambda: "ABC123",
        render_reminder=lambda kind, event, registration: "Напоминание",
    )

    assert returned is registration
    assert returned.event is event


def test_get_user_roles_returns_all_roles(storage):
    storage.ensure_role(501, "organizer")
    storage.ensure_role(501, "admin")

    assert storage.get_user_roles(501) == {"admin", "organizer"}
    assert storage.get_user_roles(999) == set()


def test_clear_user_draft_state_removes_builder_and_pending_image(storage, fixed_now):
    event = create_event(storage, fixed_now)
    storage.ensure_organizer_event(501, event.id)
    state = OrganizerState(
        user_id=501,
        mode="create",
        event_id=event.id,
        step="title",
        data={},
        updated_at=fixed_now,
    )
    storage.set_organizer_state(state)
    storage.set_pending_event_image(501, event.id, now=fixed_now)

    assert storage.get_organizer_state(501) is not None
    assert storage.get_pending_event_image(501) == event.id

    storage.clear_user_draft_state(501)

    assert storage.get_organizer_state(501) is None
    assert storage.get_pending_event_image(501) is None


def test_list_user_registrations_can_skip_unneeded_related_objects(storage, fixed_now, monkeypatch):
    plain_event = create_event(storage, fixed_now, title="Лекция")
    slotted_event = create_event(storage, fixed_now, title="Экскурсия", with_slots=True)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=iter(["PLAIN1", "SLOT02"]).__next__,
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "docs")
    service.create_registration(101, plain_event.id, None)
    service.create_registration(101, slotted_event.id, slotted_event.slots[1].id)

    def fail_slots(*args, **kwargs):
        raise AssertionError("Облегченный список записей не должен грузить слоты мероприятий")

    def fail_image(*args, **kwargs):
        raise AssertionError("Облегченный список записей не должен грузить картинки")

    monkeypatch.setattr(storage, "_event_slots_for_events", fail_slots)
    monkeypatch.setattr(storage, "_attach_event_images_for_events", fail_image)

    registrations = storage.list_user_registrations(
        101,
        with_event_slots=False,
        with_slot=False,
        with_user=False,
        with_images=False,
    )

    by_code = {registration.code: registration for registration in registrations}
    assert by_code["PLAIN1"].event.title == "Лекция"
    assert by_code["PLAIN1"].slot is None
    assert by_code["PLAIN1"].user is None
    assert by_code["SLOT02"].event.title == "Экскурсия"
    assert by_code["SLOT02"].slot is None
    assert by_code["SLOT02"].user is None
