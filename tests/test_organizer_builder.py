from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.handlers import BotHandlers
from app.bot.payloads import Payload
from app.domain import BotDomainError
from app.enums import EventFormat, LateCancelPolicy, NotificationKind
from app.storage.entities import Event, EventSlot
from tests.conftest import create_event


async def test_organizer_event_menu_uses_new_layout_and_image(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="День открытых дверей")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    storage.set_event_image(
        501,
        event.id,
        token="image-token",
        url="https://max.example/image.png",
        now=fixed_now,
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
    user_card_text = fake_bot.sent[-1]["text"]

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_event", event_id=event.id).pack(),
    )

    message = fake_bot.sent[-1]
    assert message["attachments"][0] == {
        "type": "image",
        "payload": {"token": "image-token"},
    }
    assert message["attachments"][-1]["type"] == "inline_keyboard"
    assert message["text"].startswith("🧑‍💼 МЕНЮ ОРГАНИЗАТОРА")
    assert user_card_text in message["text"]
    assert "✅ Свободных мест: 2 из 2" in message["text"]
    assert "↩️ Отмена:" not in message["text"]
    button_texts = _button_texts(message)
    assert "🗓 Изменить дату или время" in button_texts
    assert "📍 Изменить место" in button_texts
    assert "📝 Заполнить информацию заново" in button_texts
    assert "🔗 Поделиться" in button_texts
    assert "⬅️ Назад" in button_texts
    assert "Картинка" not in button_texts
    assert "Закрыть регистрацию" not in button_texts


async def test_organizer_event_menu_shows_slot_capacity_as_current_of_maximum(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Экскурсия", with_slots=True)
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "SLOT01",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.create_registration(101, event.id, event.slots[0].id)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_event", event_id=event.id).pack(),
    )

    assert "✅ Свободных мест: 1 из 2" in fake_bot.sent[-1]["text"]


async def test_organizer_menu_allows_role_to_create_event_without_existing_events(
    storage,
    fake_bot,
    fixed_now,
):
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_message(501, "Организатор", 9003, "/organizer")

    message = fake_bot.sent[-1]
    assert "🧑‍💼📚 Книга мероприятий Организатора" in message["text"]
    assert "Пока в книге Организатора нет мероприятий" in message["text"]
    assert "Создать мероприятие" in _button_texts(message)
    assert _has_local_organizer_menu_image(message)
    assert "нет доступа" not in message["text"].lower()


async def test_organizer_changes_location_and_enqueues_notification(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Пробное занятие")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "ABC123",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.create_registration(101, event.id, None)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_place", event_id=event.id).pack(),
    )
    await handlers.handle_message(
        501,
        "Организатор",
        9003,
        "Новый корпус, аудитория 404",
    )

    updated = storage.get_event(event.id)
    assert updated is not None
    assert updated.location_or_url == "Новый корпус, аудитория 404"
    assert any(
        item.kind == NotificationKind.VENUE_CHANGED
        for item in storage.list_notifications()
    )
    assert "🧑‍💼 МЕНЮ ОРГАНИЗАТОРА" in fake_bot.sent[-1]["text"]


async def test_organizer_reminder_without_slots_opens_text_prompt_immediately(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Пробное занятие")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_remind", event_id=event.id).pack(),
    )

    message = fake_bot.sent[-1]
    assert "Отправьте текст напоминания" in message["text"]
    assert "Использовать автотекст" in _button_texts(message)
    assert "Всем записавшимся" not in _button_texts(message)
    state = storage.get_organizer_state(501)
    assert state is not None
    assert state.mode == "manual_reminder_text"
    assert state.data["slot_id"] is None


async def test_organizer_reminder_with_slots_allows_scope_and_custom_text(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(
        storage,
        fixed_now,
        title="Экскурсия по лабораториям",
        with_slots=True,
    )
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "REM101",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.create_registration(101, event.id, event.slots[0].id)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_remind", event_id=event.id).pack(),
    )
    scope_message = fake_bot.sent[-1]
    scope_buttons = _buttons(scope_message)
    assert "Кому отправить напоминание" in scope_message["text"]
    assert any(button["text"] == "🔔 Всем записавшимся" for button in scope_buttons)
    assert any("10:00" in button["text"] for button in scope_buttons)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload(
            "org_remind_slot",
            event_id=event.id,
            slot_id=event.slots[0].id,
        ).pack(),
    )
    await handlers.handle_message(
        501,
        "Организатор",
        9003,
        "Ждём вас у входа в первый корпус.",
    )

    manual_items = [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.MANUAL_REMINDER
    ]
    assert len(manual_items) == 1
    assert "Ждём вас у входа в первый корпус." in manual_items[0].message_text
    assert "Начало: 24.05.2026 12:00 (через 3 дня)" in manual_items[0].message_text
    assert "Код записи: REM101" in manual_items[0].message_text
    assert "Напоминание поставлено в очередь для 1 участников." in fake_bot.sent[-1]["text"]


async def test_organizer_reminder_auto_button_uses_default_text(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Пробное занятие")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "AUTO01",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.create_registration(101, event.id, None)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_remind", event_id=event.id).pack(),
    )
    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_remind_auto").pack(),
    )

    manual_items = [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.MANUAL_REMINDER
    ]
    assert len(manual_items) == 1
    assert "скоро начнётся" not in manual_items[0].message_text
    assert "🔔 Напоминание о мероприятии" in manual_items[0].message_text
    assert "Начало: 24.05.2026 12:00 (через 3 дня)" in manual_items[0].message_text
    assert "Код записи: AUTO01" in manual_items[0].message_text


async def test_builder_creates_event_with_slots_and_image(
    storage,
    fake_bot,
    fixed_now,
):
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_create").pack(),
    )
    await handlers.handle_message(501, "Организатор", 9003, "Новый день ИТ")
    await handlers.handle_message(501, "Организатор", 9003, "Описание для гостей")
    await handlers.handle_message(501, "Организатор", 9003, "Код записи на входе")
    await handlers.handle_message(501, "Организатор", 9003, "15.06.2026")
    await handlers.handle_message(501, "Организатор", 9003, "10:30")
    await handlers.handle_message(501, "Организатор", 9003, "90")
    await handlers.handle_message(501, "Организатор", 9003, "40")
    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_builder_format", value=EventFormat.IN_PERSON.value).pack(),
    )
    await handlers.handle_message(501, "Организатор", 9003, "Главный корпус")
    await handlers.handle_message(501, "Организатор", 9003, "10:30")
    await handlers.handle_message(501, "Организатор", 9003, "11:15")
    await handlers.handle_message(501, "Организатор", 9003, "20")
    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_builder_slots_done").pack(),
    )
    await handlers.handle_message(
        501,
        "Организатор",
        9003,
        "",
        attachments=[
            {
                "type": "image",
                "payload": {
                    "token": "new-image-token",
                    "url": "https://max.example/new.png",
                },
            }
        ],
    )

    events = storage.list_organizer_events(501)
    assert len(events) == 1
    created = events[0]
    assert created.title == "Новый день ИТ"
    assert created.description == "Описание для гостей"
    assert created.requirements == "Код записи на входе"
    assert created.starts_at == datetime(2026, 6, 15, 7, 30, tzinfo=timezone.utc)
    assert created.duration_minutes == 90
    assert created.capacity_total == 40
    assert created.format == EventFormat.IN_PERSON
    assert created.location_or_url == "Главный корпус"
    assert created.cancellation_policy_text == ""
    assert created.late_cancel_policy == LateCancelPolicy.DENY
    assert len(created.slots) == 1
    assert created.slots[0].capacity == 20
    assert created.image_token == "new-image-token"
    assert "🧑‍💼 МЕНЮ ОРГАНИЗАТОРА" in fake_bot.sent[-1]["text"]


async def test_builder_rejects_past_event_date(storage, fake_bot, fixed_now):
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(501, "Организатор", 9003, Payload("org_create").pack())
    await handlers.handle_message(501, "Организатор", 9003, "Новый день ИТ")
    await handlers.handle_message(501, "Организатор", 9003, "Описание")
    await handlers.handle_message(501, "Организатор", 9003, "Требования")
    await handlers.handle_message(501, "Организатор", 9003, "20.05.2026")

    state = storage.get_organizer_state(501)
    assert state is not None
    assert state.step == "date"
    assert "Дата уже прошла" in fake_bot.sent[-1]["text"]
    assert storage.list_organizer_events(501) == []


async def test_builder_rejects_today_time_that_already_passed_in_moscow(
    storage,
    fake_bot,
    fixed_now,
):
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(501, "Организатор", 9003, Payload("org_create").pack())
    await handlers.handle_message(501, "Организатор", 9003, "Новый день ИТ")
    await handlers.handle_message(501, "Организатор", 9003, "Описание")
    await handlers.handle_message(501, "Организатор", 9003, "Требования")
    await handlers.handle_message(501, "Организатор", 9003, "21.05.2026")
    await handlers.handle_message(501, "Организатор", 9003, "11:59")

    state = storage.get_organizer_state(501)
    assert state is not None
    assert state.step == "time"
    assert "Время уже прошло" in fake_bot.sent[-1]["text"]
    assert storage.list_organizer_events(501) == []


async def test_builder_accepts_today_future_time_in_moscow(
    storage,
    fake_bot,
    fixed_now,
):
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(501, "Организатор", 9003, Payload("org_create").pack())
    await handlers.handle_message(501, "Организатор", 9003, "Новый день ИТ")
    await handlers.handle_message(501, "Организатор", 9003, "Описание")
    await handlers.handle_message(501, "Организатор", 9003, "Требования")
    await handlers.handle_message(501, "Организатор", 9003, "21.05.2026")
    await handlers.handle_message(501, "Организатор", 9003, "12:01")

    state = storage.get_organizer_state(501)
    assert state is not None
    assert state.step == "duration"
    assert state.data["time"] == "12:01"


async def test_edit_datetime_rejects_past_combination(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Пробное занятие")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_edit_date", event_id=event.id).pack(),
    )
    await handlers.handle_message(501, "Организатор", 9003, "20.05.2026")

    state = storage.get_organizer_state(501)
    assert state is not None
    assert state.mode == "edit_date"
    assert storage.get_event(event.id).starts_at == event.starts_at
    assert "Дата и время уже прошли" in fake_bot.sent[-1]["text"]


async def test_builder_slot_prompt_explains_slots_and_uses_consistent_buttons(
    storage,
    fake_bot,
    fixed_now,
):
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(501, "Организатор", 9003, Payload("org_create").pack())
    await handlers.handle_message(501, "Организатор", 9003, "Новый день ИТ")
    await handlers.handle_message(501, "Организатор", 9003, "Описание")
    await handlers.handle_message(501, "Организатор", 9003, "Требования")
    await handlers.handle_message(501, "Организатор", 9003, "15.06.2026")
    await handlers.handle_message(501, "Организатор", 9003, "10:30")
    await handlers.handle_message(501, "Организатор", 9003, "90")
    await handlers.handle_message(501, "Организатор", 9003, "40")
    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_builder_format", value=EventFormat.IN_PERSON.value).pack(),
    )
    await handlers.handle_message(501, "Организатор", 9003, "Главный корпус")

    message = fake_bot.sent[-1]
    assert "🧩 Слоты" in message["text"]
    assert "отдельные временные окна" in message["text"]
    assert "🚫 Без слотов" in _button_texts(message)
    assert "➕ Добавить слот" in _button_texts(message)
    assert "Добавьте слоты" not in message["text"]


async def test_builder_can_be_cancelled_only_after_confirmation(
    storage,
    fake_bot,
    fixed_now,
):
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(501, "Организатор", 9003, Payload("org_create").pack())
    assert "❌ Отменить создание" in _button_texts(fake_bot.sent[-1])

    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_builder_cancel").pack(),
    )
    assert "Точно отменить заполнение" in fake_bot.sent[-1]["text"]
    assert "✅ ДА, ОТМЕНИТЬ" in _button_texts(fake_bot.sent[-1])
    assert "↩️ НЕТ, ВЕРНУТЬСЯ НАЗАД" in _button_texts(fake_bot.sent[-1])
    assert storage.get_organizer_state(501) is not None

    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_builder_cancel_back").pack(),
    )
    assert "Введите название мероприятия" in fake_bot.sent[-1]["text"]
    assert storage.get_organizer_state(501) is not None

    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_builder_cancel").pack(),
    )
    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_builder_cancel_confirm").pack(),
    )

    assert storage.get_organizer_state(501) is None
    assert "Книга мероприятий Организатора" in fake_bot.sent[-1]["text"]


async def test_rebuild_builder_can_take_current_values(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Старое название")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_rebuild", event_id=event.id).pack(),
    )
    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_builder_current").pack(),
    )

    state = storage.get_organizer_state(501)
    assert state is not None
    assert state.step == "description"
    assert state.data["title"] == "Старое название"
    assert "♻️ ВЗЯТЬ ТЕКУЩЕЕ" in _button_texts(fake_bot.sent[-1])


def test_replace_event_rejects_destructive_slot_change_with_active_registrations(
    storage,
    fixed_now,
):
    event = create_event(
        storage,
        fixed_now,
        title="Слотированное событие",
        with_slots=True,
    )
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    storage.upsert_user(101, "Анна")
    storage.record_profile_consent(101, "docs", now=fixed_now)
    storage.create_registration(
        user_id=101,
        event_id=event.id,
        slot_id=event.slots[0].id,
        now=fixed_now,
        code_generator=lambda: "ABC123",
        render_reminder=lambda *_: "Напоминание",
    )
    replacement = Event(
        id=event.id,
        title=event.title,
        description=event.description,
        requirements=event.requirements,
        starts_at=event.starts_at + timedelta(days=1),
        duration_minutes=event.duration_minutes,
        format=event.format,
        location_or_url=event.location_or_url,
        cancellation_policy_text=event.cancellation_policy_text,
        capacity_total=event.capacity_total,
        late_cancel_policy=event.late_cancel_policy,
    )
    new_slots = [
        EventSlot(
            id=0,
            event_id=event.id,
            title="12:00",
            starts_at=event.starts_at + timedelta(days=1, hours=2),
            ends_at=event.starts_at + timedelta(days=1, hours=3),
            capacity=10,
        )
    ]

    with pytest.raises(BotDomainError):
        storage.replace_organizer_event(
            501,
            replacement,
            slots=new_slots,
            image_token=None,
            image_url=None,
            now=fixed_now,
        )


def _button_texts(message: dict) -> str:
    return " ".join(button["text"] for button in _buttons(message))


def _buttons(message: dict) -> list[dict]:
    return [
        button
        for attachment in message["attachments"]
        if isinstance(attachment, dict) and attachment["type"] == "inline_keyboard"
        for row in attachment["payload"]["buttons"]
        for button in row
    ]


def _has_local_organizer_menu_image(message: dict) -> bool:
    return any(
        getattr(attachment, "path", "").replace("\\", "/").endswith(
            "app/assets/organizer-menu.png"
        )
        for attachment in message["attachments"]
    )
