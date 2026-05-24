from __future__ import annotations

from pathlib import Path

from app.test_reminder import enqueue_test_reminders, format_simulated_starts_in
from app.services.registration import RegistrationService
from tests.conftest import create_event


ROOT = Path(__file__).resolve().parents[1]


def test_format_simulated_starts_in_uses_russian_units():
    assert format_simulated_starts_in(45) == "45 минут"
    assert format_simulated_starts_in(60) == "1 час"
    assert format_simulated_starts_in(120) == "2 часа"
    assert format_simulated_starts_in(1440) == "1 день"


def test_enqueue_test_reminders_uses_simulated_starts_in_text(storage, fixed_now):
    event = create_event(storage, fixed_now, title="Пробное занятие")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "TEST01",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "docs")
    service.create_registration(101, event.id, None)

    created = enqueue_test_reminders(
        storage,
        actor_user_id=501,
        event_id=event.id,
        slot_id=None,
        starts_in_minutes=120,
        now=lambda: fixed_now,
    )

    assert len(created) == 1
    assert "примерно через 2 часа" in created[0].message_text
    assert "Код записи: TEST01" in created[0].message_text


def test_powershell_test_reminder_script_invokes_python_module():
    script = (ROOT / "scripts" / "send-test-reminder.ps1").read_text(encoding="utf-8")

    assert "app.test_reminder" in script
    assert "--actor-user-id" in script
    assert "--event-id" in script
    assert "--minutes" in script
