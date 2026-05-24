from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from itertools import count
from threading import Lock

import pytest

from app.domain import (
    DuplicateActiveRegistrationError,
    LateCancellationDeniedError,
    NoSeatsAvailableError,
)
from app.enums import LateCancelPolicy, OutboxStatus, RegistrationStatus
from app.services.registration import RegistrationService
from tests.conftest import create_event


def test_profile_consent_is_recorded_with_document_version(storage, fixed_now):
    service = RegistrationService(storage, now=lambda: fixed_now)

    service.upsert_user(101, "Анна Абитуриент")
    assert service.has_profile_consent(101) is False

    consent = service.record_profile_consent(101, document_version="hackathon-2026-05")

    assert consent.user_id == 101
    assert consent.document_version == "hackathon-2026-05"
    assert consent.profile_data_allowed is True
    assert service.has_profile_consent(101) is True


def test_registration_creates_code_schedules_reminders_and_blocks_duplicates(
    storage, fixed_now
):
    event = create_event(storage, fixed_now, capacity=1)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "ABC123",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "hackathon-2026-05")

    registration = service.create_registration(101, event.id, None)

    assert registration.code == "ABC123"
    assert registration.status == RegistrationStatus.CONFIRMED
    assert registration.notifications_enabled is True
    outbox = storage.list_notifications()
    assert [item.kind.value for item in outbox] == ["reminder_24h", "reminder_1h"]
    assert all(item.status == OutboxStatus.PENDING for item in outbox)

    with pytest.raises(DuplicateActiveRegistrationError):
        service.create_registration(101, event.id, None)

    service.upsert_user(102, "Борис")
    service.record_profile_consent(102, "hackathon-2026-05")
    with pytest.raises(NoSeatsAvailableError):
        service.create_registration(102, event.id, None)


def test_cancel_before_start_returns_seat_to_pool(storage, fixed_now):
    event = create_event(storage, fixed_now, capacity=1)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=iter(["AAA111", "BBB222"]).__next__,
    )
    for user_id, name in [(101, "Анна"), (102, "Борис")]:
        service.upsert_user(user_id, name)
        service.record_profile_consent(user_id, "hackathon-2026-05")

    first = service.create_registration(101, event.id, None)
    service.cancel_registration(101, first.id)
    second = service.create_registration(102, event.id, None)

    assert first.status == RegistrationStatus.CANCELED_BY_USER
    assert second.code == "BBB222"
    assert service.available_places(event.id, None) == 0


def test_repeated_cancellation_does_not_increase_available_places(storage, fixed_now):
    event = create_event(storage, fixed_now, capacity=1)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "AAA111",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "hackathon-2026-05")
    registration = service.create_registration(101, event.id, None)

    service.cancel_registration(101, registration.id)
    service.cancel_registration(101, registration.id)

    assert service.available_places(event.id, None) == 1
    assert storage.get_event(event.id).booked_count == 0


def test_late_cancellation_policy_is_enforced(storage, fixed_now):
    denied_event = create_event(
        storage,
        fixed_now,
        title="Событие без поздней отмены",
        starts_in=timedelta(hours=1),
        late_policy=LateCancelPolicy.DENY,
    )
    allowed_event = create_event(
        storage,
        fixed_now,
        title="Событие с поздней отменой",
        starts_in=timedelta(hours=1),
        late_policy=LateCancelPolicy.ALLOW_LATE,
    )
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=iter(["DENIED", "LATE42"]).__next__,
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "hackathon-2026-05")
    denied = service.create_registration(101, denied_event.id, None)
    allowed = service.create_registration(101, allowed_event.id, None)
    storage.update_event_start(denied_event.id, fixed_now - timedelta(hours=1))
    storage.update_event_start(allowed_event.id, fixed_now - timedelta(hours=1))

    with pytest.raises(LateCancellationDeniedError):
        service.cancel_registration(101, denied.id)
    service.cancel_registration(101, allowed.id)

    assert denied.status == RegistrationStatus.CONFIRMED
    assert allowed.status == RegistrationStatus.LATE_CANCELED


def test_slot_capacity_is_counted_per_slot(storage, fixed_now):
    event = create_event(storage, fixed_now, capacity=10, with_slots=True)
    slots = event.slots
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=iter(["SLOT01", "SLOT02"]).__next__,
    )
    for user_id in [101, 102]:
        service.upsert_user(user_id, f"User {user_id}")
        service.record_profile_consent(user_id, "hackathon-2026-05")

    service.create_registration(101, event.id, slots[0].id)

    with pytest.raises(NoSeatsAvailableError):
        service.create_registration(102, event.id, slots[0].id)

    second = service.create_registration(102, event.id, slots[1].id)
    assert second.slot_id == slots[1].id


def test_concurrent_registration_on_last_seat_creates_only_one_active_record(
    storage, fixed_now
):
    event = create_event(storage, fixed_now, capacity=1)
    code_counter = count(1)
    code_lock = Lock()

    def next_code() -> str:
        with code_lock:
            return f"C{next(code_counter):05d}"

    for user_id in [101, 102]:
        service = RegistrationService(storage, now=lambda: fixed_now)
        service.upsert_user(user_id, f"User {user_id}")
        service.record_profile_consent(user_id, "hackathon-2026-05")

    def register(user_id: int):
        service = RegistrationService(
            storage,
            now=lambda: fixed_now,
            code_generator=next_code,
        )
        return service.create_registration(user_id, event.id, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda uid: _capture_error(register, uid), [101, 102]))

    successes = [item for item in results if not isinstance(item, Exception)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], NoSeatsAvailableError)
    assert service.available_places(event.id, None) == 0
    assert storage.get_event(event.id).booked_count == 1


def test_concurrent_registration_on_last_slot_creates_only_one_record(
    storage, fixed_now
):
    event = create_event(storage, fixed_now, capacity=10, with_slots=True)
    slot = event.slots[0]
    code_counter = count(1)
    code_lock = Lock()

    def next_code() -> str:
        with code_lock:
            return f"S{next(code_counter):05d}"

    for user_id in [101, 102]:
        service = RegistrationService(storage, now=lambda: fixed_now)
        service.upsert_user(user_id, f"User {user_id}")
        service.record_profile_consent(user_id, "hackathon-2026-05")

    def register(user_id: int):
        service = RegistrationService(
            storage,
            now=lambda: fixed_now,
            code_generator=next_code,
        )
        return service.create_registration(user_id, event.id, slot.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda uid: _capture_error(register, uid), [101, 102]))

    successes = [item for item in results if not isinstance(item, Exception)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], NoSeatsAvailableError)
    assert service.available_places(event.id, slot.id) == 0
    assert storage.get_event(event.id).slots[0].booked_count == 1


def test_notification_toggle_is_stored_on_registration(storage, fixed_now):
    event = create_event(storage, fixed_now)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "TOGGLE",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "hackathon-2026-05")
    registration = service.create_registration(101, event.id, None)

    service.set_notifications_enabled(101, registration.id, enabled=False)

    stored = storage.get_registration(registration.id)
    assert stored is not None
    assert stored.notifications_enabled is False


def _capture_error(func, *args):
    try:
        return func(*args)
    except Exception as exc:
        return exc
