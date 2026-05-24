from __future__ import annotations

from datetime import timedelta

from app.bot.handlers import BotHandlers
from app.bot.payloads import Payload
from app.enums import NotificationKind
from app.services.event_cleanup import EventCleanupService
from app.services.registration import RegistrationService
from app.storage.entities import NotificationOutbox
from tests.conftest import create_event


async def test_user_registrations_hide_started_events(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Уже началось")
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "START1",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "docs")
    service.create_registration(101, event.id, None)
    storage.update_event_start(event.id, fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("my_regs").pack(),
    )

    assert fake_bot.sent[-1]["text"] == "🎫 У вас пока нет записей."


async def test_user_event_detail_hides_started_event(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Уже началось")
    storage.update_event_start(event.id, fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )

    assert "Мероприятие уже началось или завершилось" in fake_bot.sent[-1]["text"]
    assert "ℹ️ Уже началось" not in fake_bot.sent[-1]["text"]


async def test_organizer_menu_separates_past_events_and_shows_days_until_cleanup(
    storage,
    fake_bot,
    fixed_now,
):
    upcoming = create_event(storage, fixed_now, title="Будущее")
    past = create_event(storage, fixed_now, title="Прошедшее", starts_in=timedelta(days=-2))
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, upcoming.id)
    storage.ensure_organizer_event(501, past.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_message(501, "Организатор", 9003, "/organizer")

    text = fake_bot.sent[-1]["text"]
    assert "🧑‍💼📚 Книга мероприятий Организатора" in text
    assert "🔥 БЛИЖАЙШИЕ" in text
    assert "1. Будущее" in text
    assert "🕘 ПРОШЕДШИЕ" in text
    assert "2. Прошедшее" in text
    assert "🧹 Удалится через 5 дн." in text


def test_cleanup_removes_expired_event_and_related_records(storage, fixed_now):
    event = create_event(storage, fixed_now, title="Старое событие")
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "OLD777",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "docs")
    registration = service.create_registration(101, event.id, None)
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    storage.assign_event_slug(event.id, "old-event", now=fixed_now)
    storage.set_event_image(
        501,
        event.id,
        token="image-token",
        url="https://max.example/old.png",
        now=fixed_now,
    )
    storage.set_pending_event_image(501, event.id, now=fixed_now)
    storage.add_notification(
        NotificationOutbox(
            id=0,
            event_id=event.id,
            registration_id=registration.id,
            user_id=101,
            kind=NotificationKind.VENUE_CHANGED,
            message_text="Устарело.",
            send_after=fixed_now,
        )
    )
    storage.update_event_start(event.id, fixed_now - timedelta(days=8))

    removed_count = EventCleanupService(storage, now=lambda: fixed_now).cleanup()

    assert removed_count == 1
    assert storage.get_event(event.id) is None
    assert storage.get_event_by_slug("old-event") is None
    assert storage.get_registration(registration.id) is None
    assert storage.find_registration_by_code_global("OLD777") is None
    assert storage.get_pending_event_image(501) is None
    assert storage.list_notifications() == []
    assert storage.list_organizer_events(501) == []


def test_cleanup_keeps_recently_started_event_for_organizer(storage, fixed_now):
    event = create_event(storage, fixed_now, title="Недавнее", starts_in=timedelta(days=-6))
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)

    removed_count = EventCleanupService(storage, now=lambda: fixed_now).cleanup()

    assert removed_count == 0
    assert storage.get_event(event.id) is not None
    assert [item.id for item in storage.list_organizer_events(501)] == [event.id]
