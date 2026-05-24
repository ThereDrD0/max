from __future__ import annotations

from datetime import timedelta

from app.bot.handlers import BotHandlers
from app.bot.payloads import Payload
from app.services.registration import RegistrationService
from tests.conftest import create_event


async def test_deeplink_new_user_accepts_consent_and_opens_target_event(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="День открытых дверей ИТ")
    storage.assign_event_slug(event.id, "it-open-day-2026-06-15", now=fixed_now)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        max_bot_username="id123_bot",
    )

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="e_it-open-day-2026-06-15")

    first_message = fake_bot.sent[-1]
    assert "командой хакатона" in first_message["text"]
    assert "После согласия открою мероприятие: «День открытых дверей ИТ»" in first_message["text"]
    assert "Согласен и открыть мероприятие" in str(first_message["attachments"])

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("consent_accept_event", value="it-open-day-2026-06-15").pack(),
    )

    detail = fake_bot.sent[-1]
    deeplink = "https://max.ru/id123_bot?start=e_it-open-day-2026-06-15"
    assert "ℹ️ День открытых дверей ИТ" in detail["text"]
    assert "Ссылка: Нажмите чтобы скопировать" in detail["text"]
    assert deeplink not in detail["text"]
    buttons = _buttons(detail)
    assert buttons[0]["text"] == "📝 Записаться"
    assert buttons[1] == {
        "type": "clipboard",
        "text": "🔗 Поделиться",
        "payload": deeplink,
    }
    assert not any(button.get("type") == "link" for button in buttons)


async def test_deeplink_user_with_consent_opens_event_detail(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Онлайн-консультация")
    storage.assign_event_slug(event.id, "online-admission-2026-06-20", now=fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="e_online-admission-2026-06-20")

    assert "ℹ️ Онлайн-консультация" in fake_bot.sent[-1]["text"]


async def test_deeplink_with_slots_keeps_explicit_slot_choice(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Экскурсия", with_slots=True)
    storage.assign_event_slug(event.id, "campus-tour", now=fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="e_campus-tour")
    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_book", event_id=event.id).pack(),
    )

    assert "Выберите слот для мероприятия «Экскурсия»" in fake_bot.sent[-1]["text"]


async def test_deeplink_existing_registration_shows_code_without_booking_button(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Пробное занятие")
    storage.assign_event_slug(event.id, "python-class", now=fixed_now)
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "PY2026",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "docs")
    service.create_registration(101, event.id, None)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="e_python-class")

    message = fake_bot.sent[-1]
    assert "\n\n✅ ВЫ УЖЕ ЗАПИСАНЫ НА ЭТО МЕРОПРИЯТИЕ." in message["text"]
    assert "**" not in message["text"]
    assert "Код записи: PY2026" in message["text"]
    assert "📝 Записаться" not in _button_texts(message)


async def test_deeplink_unavailable_event_hides_booking_button(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Закрытое мероприятие")
    event.registration_closed = True
    storage.assign_event_slug(event.id, "closed-event", now=fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="e_closed-event")

    message = fake_bot.sent[-1]
    assert "Регистрация закрыта." in message["text"]
    assert "📝 Записаться" not in _button_texts(message)


async def test_deeplink_started_event_hides_booking_button(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Уже началось")
    storage.update_event_start(event.id, fixed_now - timedelta(hours=1))
    storage.assign_event_slug(event.id, "started-event", now=fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="e_started-event")

    message = fake_bot.sent[-1]
    assert "Мероприятие уже началось." in message["text"]
    assert "📝 Записаться" not in _button_texts(message)


async def test_deeplink_full_event_hides_booking_button(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Мест нет", capacity=1)
    storage.assign_event_slug(event.id, "full-event", now=fixed_now)
    service = RegistrationService(storage, now=lambda: fixed_now, code_generator=lambda: "FULL01")
    service.upsert_user(201, "Борис")
    service.record_profile_consent(201, "docs")
    service.create_registration(201, event.id, None)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="e_full-event")

    message = fake_bot.sent[-1]
    assert "Свободных мест нет." in message["text"]
    assert "📝 Записаться" not in _button_texts(message)


async def test_deeplink_invalid_payload_shows_soft_error(storage, fake_bot, fixed_now):
    create_event(storage, fixed_now, title="День открытых дверей")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="bad payload")

    message = fake_bot.sent[-1]
    assert "Ссылка на мероприятие устарела или неверна." in message["text"]
    assert "командой хакатона" in message["text"]


async def test_event_detail_omits_share_link_without_bot_username(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Без ссылки")
    storage.assign_event_slug(event.id, "no-username", now=fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_bot_started(101, "Анна", 9001, start_payload="e_no-username")

    assert "https://max.ru/" not in fake_bot.sent[-1]["text"]
    assert not any(button.get("type") in {"clipboard", "link"} for button in _buttons(fake_bot.sent[-1]))


async def test_event_detail_generates_share_link_for_existing_event_without_slug(
    storage, fake_bot, fixed_now
):
    event = create_event(
        storage,
        fixed_now,
        title="День открытых дверей ИТ-института",
    )
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        max_bot_username="id123_bot",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )

    message = fake_bot.sent[-1]
    slug = storage.get_event_slug(event.id)
    assert slug is not None
    assert "Ссылка: Нажмите чтобы скопировать" in message["text"]
    assert _clipboard_payload(message) == f"https://max.ru/id123_bot?start=e_{slug}"


async def test_event_detail_generates_unique_slug_when_default_slug_collides(
    storage, fake_bot, fixed_now
):
    first = create_event(storage, fixed_now, title="Одинаковое мероприятие")
    second = create_event(storage, fixed_now, title="Одинаковое мероприятие")
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        max_bot_username="id123_bot",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=first.id).pack(),
    )
    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=second.id).pack(),
    )

    first_slug = storage.get_event_slug(first.id)
    second_slug = storage.get_event_slug(second.id)
    assert first_slug is not None
    assert second_slug is not None
    assert second_slug != first_slug
    assert second_slug.endswith(f"-{second.id}")
    assert _clipboard_payload(fake_bot.sent[-1]) == f"https://max.ru/id123_bot?start=e_{second_slug}"


async def test_event_detail_uses_bot_username_from_client_when_env_is_empty(
    storage, fixed_now
):
    class UsernameBot:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def get_bot_username(self) -> str:
            return "id123_bot"

        async def send_message(
            self,
            *,
            user_id=None,
            chat_id=None,
            text: str,
            attachments=None,
            notify=None,
        ):
            self.sent.append(
                {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "text": text,
                    "attachments": attachments or [],
                    "notify": notify,
                }
            )
            return "mid.1"

        async def edit_message(self, *, message_id: str, text: str, attachments=None, notify=None):
            return message_id

        async def delete_message(self, *, message_id: str):
            return None

    bot = UsernameBot()
    event = create_event(storage, fixed_now, title="Текущая встреча")
    storage.assign_event_slug(event.id, "current-event", now=fixed_now)
    handlers = BotHandlers(storage, bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )

    assert _clipboard_payload(bot.sent[-1]) == "https://max.ru/id123_bot?start=e_current-event"


def _button_texts(message: dict) -> str:
    return " ".join(button["text"] for button in _buttons(message))


def _buttons(message: dict) -> list[dict]:
    return [
        button
        for attachment in message["attachments"]
        for row in attachment["payload"]["buttons"]
        for button in row
    ]


def _clipboard_payload(message: dict) -> str | None:
    for button in _buttons(message):
        if button.get("type") == "clipboard":
            return button.get("payload")
    return None
