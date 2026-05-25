from __future__ import annotations

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
