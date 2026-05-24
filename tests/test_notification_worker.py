from __future__ import annotations

from app.enums import NotificationKind, OutboxStatus
from app.services.notification_worker import NotificationWorker
from app.services.registration import RegistrationService
from app.storage.entities import NotificationOutbox
from tests.conftest import create_event


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
