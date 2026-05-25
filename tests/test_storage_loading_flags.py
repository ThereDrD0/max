from __future__ import annotations

from app.services.registration import RegistrationService
from tests.conftest import create_event


def test_get_event_can_skip_slots_and_image(storage, fixed_now, monkeypatch):
    event = create_event(storage, fixed_now, with_slots=True)

    def fail_slots(event_id: int):
        raise AssertionError("get_event не должен загружать слоты")

    def fail_image(loaded_event):
        raise AssertionError("get_event не должен загружать картинку")

    monkeypatch.setattr(storage, "_event_slots", fail_slots)
    monkeypatch.setattr(storage, "_attach_event_image", fail_image)

    assert storage.get_event(event.id, with_slots=False, with_image=False) is event


def test_list_events_can_skip_slots_and_images(storage, fixed_now, monkeypatch):
    create_event(storage, fixed_now, with_slots=True)

    def fail_slots(event_id: int):
        raise AssertionError("list_events не должен загружать слоты")

    def fail_image(loaded_event):
        raise AssertionError("list_events не должен загружать картинки")

    monkeypatch.setattr(storage, "_event_slots", fail_slots)
    monkeypatch.setattr(storage, "_attach_event_image", fail_image)

    events = storage.list_events(with_slots=False, with_images=False)

    assert len(events) == 1


def test_list_events_batches_slot_loading(storage, fixed_now, monkeypatch):
    first = create_event(storage, fixed_now, title="Первое мероприятие", with_slots=True)
    second = create_event(storage, fixed_now, title="Второе мероприятие", with_slots=True)

    def fail_slots(event_id: int):
        raise AssertionError("list_events должен загружать слоты пачкой")

    monkeypatch.setattr(storage, "_event_slots", fail_slots)

    events = storage.list_events(with_slots=True, with_images=False)

    by_id = {event.id: event for event in events}
    assert [slot.title for slot in by_id[first.id].slots] == ["10:00", "11:00"]
    assert [slot.title for slot in by_id[second.id].slots] == ["10:00", "11:00"]


def test_list_user_registrations_batches_related_objects(
    storage,
    fixed_now,
    monkeypatch,
):
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

    def fail_attach(registration):
        raise AssertionError("list_user_registrations не должен вызывать _attach_registration в цикле")

    monkeypatch.setattr(storage, "_attach_registration", fail_attach)

    registrations = storage.list_user_registrations(101)

    by_code = {registration.code: registration for registration in registrations}
    assert by_code["PLAIN1"].event.title == "Лекция"
    assert by_code["PLAIN1"].slot is None
    assert by_code["SLOT02"].event.title == "Экскурсия"
    assert by_code["SLOT02"].slot.title == "11:00"


def test_list_organizer_events_batches_event_loading(storage, fixed_now, monkeypatch):
    first = create_event(storage, fixed_now, title="Первое мероприятие")
    second = create_event(storage, fixed_now, title="Второе мероприятие")
    storage.ensure_organizer_event(501, first.id)
    storage.ensure_organizer_event(501, second.id)

    def fail_get_event(*args, **kwargs):
        raise AssertionError("list_organizer_events не должен вызывать get_event в цикле")

    monkeypatch.setattr(storage, "get_event", fail_get_event)

    events = storage.list_organizer_events(501, with_slots=False, with_images=False)

    assert [event.title for event in events] == [
        "Первое мероприятие",
        "Второе мероприятие",
    ]
