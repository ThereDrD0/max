from __future__ import annotations

from app.enums import NotificationKind, OutboxStatus
from app.services.notification_worker import NotificationWorker
from app.services.registration import RegistrationService
from app.storage.entities import NotificationOutbox
from tests.conftest import create_event


async def test_notification_worker_skips_repair_sync_outside_window_but_sends_due(
    storage,
    fake_bot,
    fixed_now,
    monkeypatch,
):
    event = create_event(storage, fixed_now, title="Консультация по приёму", capacity=10)
    storage.add_notification(
        NotificationOutbox(
            id=0,
            event_id=event.id,
            registration_id=None,
            user_id=101,
            kind=NotificationKind.VENUE_CHANGED,
            message_text="Изменилась аудитория.",
            send_after=fixed_now,
        )
    )

    def fail_sync(*args, **kwargs):
        raise AssertionError("Обычный timer вне sync-окна не должен запускать repair-синхронизацию")

    monkeypatch.setattr(storage, "sync_registration_reminders", fail_sync)
    worker = NotificationWorker(
        storage,
        fake_bot,
        now=lambda: fixed_now.replace(minute=30),
        max_rps=1000,
        reminder_sync_interval_minutes=60,
        reminder_sync_window_minutes=5,
    )

    sent_count = await worker.process_due(limit=10)

    assert sent_count == 1
    assert fake_bot.sent[-1]["text"] == "Изменилась аудитория."


async def test_notification_worker_runs_repair_sync_inside_window(
    storage,
    fake_bot,
    fixed_now,
    monkeypatch,
):
    sync_calls = []

    def sync_registration_reminders(**kwargs):
        sync_calls.append(kwargs)
        return 0

    monkeypatch.setattr(storage, "sync_registration_reminders", sync_registration_reminders)
    worker = NotificationWorker(
        storage,
        fake_bot,
        now=lambda: fixed_now.replace(minute=1),
        max_rps=1000,
        reminder_sync_interval_minutes=60,
        reminder_sync_window_minutes=5,
    )

    sent_count = await worker.process_due(limit=10)

    assert sent_count == 0
    assert len(sync_calls) == 1


async def test_notification_worker_sends_due_items_and_skips_disabled_registration(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Консультация по приёму", capacity=10)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "NOTE01",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "hackathon-2026-05")
    registration = service.create_registration(101, event.id, None)
    service.set_notifications_enabled(101, registration.id, enabled=False)
    storage.add_notification(
        NotificationOutbox(
            id=0,
            event_id=event.id,
            registration_id=registration.id,
            user_id=101,
            kind=NotificationKind.VENUE_CHANGED,
            message_text="Изменилась аудитория.",
            send_after=fixed_now,
        )
    )

    worker = NotificationWorker(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        max_rps=1000,
    )
    sent_count = await worker.process_due(limit=10)

    manual_item = storage.list_notifications()[-1]
    assert sent_count == 0
    assert fake_bot.sent == []
    assert manual_item.status == OutboxStatus.SKIPPED


async def test_notification_worker_sends_reminder_with_image_and_detail_button(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Консультация по приёму", capacity=10)
    storage.assign_event_slug(event.id, "consultation", now=fixed_now)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "NOTE01",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "hackathon-2026-05")
    service.create_registration(101, event.id, None)

    worker = NotificationWorker(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        max_rps=1000,
        max_bot_username="id123_bot",
    )
    sent_count = await worker.process_due(limit=10)

    assert sent_count == 1
    message = fake_bot.sent[-1]
    assert "🔔 Напоминание о мероприятии" in message["text"]
    assert "📅 Начало: 24.05.2026 12:00 (через 3 дня)" in message["text"]
    assert any(
        isinstance(attachment, dict)
        and attachment.get("type") == "image"
        and isinstance((attachment.get("payload") or {}).get("token"), str)
        and bool((attachment.get("payload") or {}).get("token"))
        for attachment in message["attachments"]
    )
    assert _detail_button(message) == {
        "type": "link",
        "text": "ℹ️ Подробнее",
        "url": "https://max.ru/id123_bot?start=e_consultation",
    }


def _detail_button(message: dict) -> dict | None:
    for attachment in message["attachments"]:
        if not isinstance(attachment, dict) or attachment.get("type") != "inline_keyboard":
            continue
        for row in attachment["payload"]["buttons"]:
            for button in row:
                if button.get("text") == "ℹ️ Подробнее":
                    return button
    return None
