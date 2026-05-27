from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.handlers import BotHandlers
from app.bot.payloads import Payload
from app.domain import BotDomainError
from app.enums import EventFormat, LateCancelPolicy, NotificationKind, RegistrationStatus
from app.storage.entities import Event, EventSlot
from app.storage.memory import MemoryStorage
from tests.conftest import create_event


class CountingRegistrationStorage(MemoryStorage):
    def __init__(self) -> None:
        super().__init__()
        self.event_registration_reads = 0

    def get_event_registrations(self, *args, **kwargs):
        self.event_registration_reads += 1
        return super().get_event_registrations(*args, **kwargs)


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
    handlers.registration_service.create_registration(101, event.id, None)

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
    assert "✅ Свободных мест: 1 из 2" in message["text"]
    assert "↩️ Отмена:" not in message["text"]
    button_texts = _button_texts(message)
    rows = _keyboard_rows(message)
    assert [button["text"] for button in rows[0]] == [
        "🗓 Изменить дату или время",
        "📍 Изменить место",
    ]
    assert "🗓 Изменить дату или время" in button_texts
    assert "📍 Изменить место" in button_texts
    assert "📝 Заполнить информацию заново" in button_texts
    assert any(
        [button["text"] for button in row]
        == ["🚫 Закрыть регистрацию", "🛑 Закрыть мероприятие"]
        for row in rows
    )
    assert any(
        [button["text"] for button in row]
        == ["🔔 Напомнить участникам", "🔗 Поделиться"]
        for row in rows
    )
    assert any(
        [button["text"] for button in row]
        == ["👥 Участники", "🔎 Отметить по коду"]
        for row in rows
    )
    assert "👥 Участники" in button_texts
    assert "🔎 Отметить по коду" in button_texts
    assert "🚫 Закрыть регистрацию" in button_texts
    assert "🛑 Закрыть мероприятие" in button_texts
    assert "🔗 Поделиться" in button_texts
    assert "⬅️ Назад" in button_texts
    assert "Картинка" not in button_texts


async def test_organizer_event_menu_hides_participants_button_without_registrations(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="День открытых дверей")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_event", event_id=event.id).pack(),
    )

    button_texts = _button_texts(fake_bot.sent[-1])
    assert "👥 Участники" not in button_texts
    assert "🔎 Отметить по коду" not in button_texts


async def test_organizer_datetime_menu_does_not_load_participants(fake_bot, fixed_now):
    storage = CountingRegistrationStorage()
    event = create_event(storage, fixed_now, title="День открытых дверей")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_datetime", event_id=event.id).pack(),
    )

    assert storage.event_registration_reads == 0


async def test_organizer_close_registration_requires_confirmation_and_hides_action(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="День открытых дверей")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_close_confirm", event_id=event.id).pack(),
    )

    confirmation = fake_bot.sent[-1]
    assert (
        "Закрыть регистрацию на мероприятие «День открытых дверей»?"
        in confirmation["text"]
    )
    assert "Новые участники больше не увидят мероприятие в каталоге" in confirmation["text"]
    assert "Текущие записи останутся действующими" in confirmation["text"]
    assert "🚫 Закрыть регистрацию" in _button_texts(confirmation)
    assert "⬅️ Назад" in _button_texts(confirmation)
    assert storage.get_event(event.id).registration_closed is False

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_close", event_id=event.id).pack(),
    )

    closed = fake_bot.sent[-1]
    assert "Регистрация на «День открытых дверей» закрыта." in closed["text"]
    assert "Новые участники больше не увидят мероприятие в каталоге" in closed["text"]
    assert "Текущие записи остаются действующими" in closed["text"]
    assert storage.get_event(event.id).registration_closed is True

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_event", event_id=event.id).pack(),
    )

    menu = fake_bot.sent[-1]
    assert "Регистрация новых участников закрыта." in menu["text"]
    assert "🚫 Закрыть регистрацию" not in _button_texts(menu)


async def test_organizer_close_event_requires_confirmation_and_notifies_participants(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="День открытых дверей")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "CLOSE1",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    registration = handlers.registration_service.create_registration(101, event.id, None)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_event_close_confirm", event_id=event.id).pack(),
    )

    confirmation = fake_bot.sent[-1]
    assert "Закрыть мероприятие «День открытых дверей»?" in confirmation["text"]
    assert "все участники получат уведомление" in confirmation["text"]
    assert "🛑 Закрыть мероприятие" in _button_texts(confirmation)
    assert "⬅️ Назад" in _button_texts(confirmation)
    assert storage.get_registration(registration.id).status.value == "confirmed"

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_event_close", event_id=event.id).pack(),
    )

    closed = fake_bot.sent[-1]
    assert "Мероприятие «День открытых дверей» закрыто." in closed["text"]
    assert "Уведомления поставлены в очередь для 1 участников." in closed["text"]
    assert storage.get_registration(registration.id).status.value == "canceled_by_organizer"
    assert storage.get_event(event.id).registration_closed is True
    event_notifications = [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.EVENT_CLOSED
    ]
    assert len(event_notifications) == 1
    assert event_notifications[0].user_id == 101
    assert event_notifications[0].registration_id is None
    assert "Мероприятие «День открытых дверей» закрыто" in event_notifications[0].message_text


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
    assert "📚 Книга мероприятий Организатора" in message["text"]
    assert "Пока в книге Организатора нет мероприятий" in message["text"]
    assert "📝 Создать мероприятие" in _button_texts(message)
    assert "➕ Создать мероприятие" not in _button_texts(message)
    assert _has_uploaded_image(message)
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
    assert "📅 Начало: 24.05.2026 12:00 (через 3 дня)" in manual_items[0].message_text
    assert "🎫 Код записи: REM101" in manual_items[0].message_text
    confirmation_index = next(
        index
        for index, message in enumerate(fake_bot.sent)
        if message["user_id"] == 501 and message["text"] == "Запускаю отправку напоминаний: 1."
    )
    participant_index = next(
        index
        for index, message in enumerate(fake_bot.sent)
        if message["user_id"] == 101 and "Ждём вас у входа в первый корпус." in message["text"]
    )
    assert confirmation_index < participant_index


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
    assert "📅 Начало: 24.05.2026 12:00 (через 3 дня)" in manual_items[0].message_text
    assert "🎫 Код записи: AUTO01" in manual_items[0].message_text
    assert manual_items[0].status.value == "sent"
    confirmation_index = next(
        index
        for index, message in enumerate(fake_bot.sent)
        if message["user_id"] == 501 and message["text"] == "Запускаю отправку напоминаний: 1."
    )
    participant_index = next(
        index
        for index, message in enumerate(fake_bot.sent)
        if message["user_id"] == 101 and "🔔 Напоминание о мероприятии" in message["text"]
    )
    assert confirmation_index < participant_index
    assert any(
        message["user_id"] == 101 and "🔔 Напоминание о мероприятии" in message["text"]
        for message in fake_bot.sent
    )


async def test_organizer_participants_book_sorts_statuses_and_shows_action_buttons(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Пробное занятие", capacity=10)
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=iter(["CONF2", "CONF1", "ATT01", "CAN01", "LATE1"]).__next__,
    )
    registrations = []
    for user_id, name in [
        (101, "Яна"),
        (102, "Анна"),
        (103, "Петр"),
        (104, "Борис"),
        (105, "Сергей"),
    ]:
        handlers.registration_service.upsert_user(user_id, name)
        handlers.registration_service.record_profile_consent(user_id, "docs")
        registrations.append(
            handlers.registration_service.create_registration(user_id, event.id, None)
        )
    handlers.organizer_service.mark_attended(501, registrations[2].id)
    handlers.organizer_service.change_status(
        501,
        registrations[3].id,
        RegistrationStatus.CANCELED_BY_ORGANIZER,
    )
    handlers.organizer_service.change_status(
        501,
        registrations[4].id,
        RegistrationStatus.LATE_CANCELED,
    )

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_participants", event_id=event.id).pack(),
    )

    message = fake_bot.sent[-1]
    assert message["format"] == "markdown"
    assert "👥 Участники мероприятия" in message["text"]
    assert "Нажмите на профиль записанного участника" in message["text"]
    assert "Страница 1/1" in message["text"]
    assert message["text"].splitlines()[-1] == "Страница 1/1"
    assert "1. [Анна](max://user/102) - CONF1 - Записан" in message["text"]
    assert "2. [Яна](max://user/101) - CONF2 - Записан" in message["text"]
    assert "3. [Петр](max://user/103) - ATT01 - Пришел" in message["text"]
    assert "4. [Борис](max://user/104) - CAN01 - Запись отменена" in message["text"]
    assert "5. [Сергей](max://user/105) - LATE1 - Запись отменена" in message["text"]
    assert message["text"].index("1. [Анна]") < message["text"].index("2. [Яна]")
    assert message["text"].index("2. [Яна]") < message["text"].index("3. [Петр]")
    button_texts = _button_texts(message)
    assert "✅ Анна пришел" in button_texts
    assert "✅ Яна пришел" in button_texts
    assert "✅ Петр пришел" not in button_texts
    assert "↩️ Петр записан" in button_texts
    assert "✅ Борис пришел" not in button_texts
    assert "✅ Сергей пришел" not in button_texts
    assert "⬅️ К мероприятию" in button_texts
    assert _has_uploaded_image(message)


async def test_organizer_participants_book_paginates_eight_items(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Пробное занятие", capacity=12)
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=iter([f"CODE{i:02d}" for i in range(1, 10)]).__next__,
    )
    for index in range(1, 10):
        user_id = 100 + index
        handlers.registration_service.upsert_user(user_id, f"Участник {index:02d}")
        handlers.registration_service.record_profile_consent(user_id, "docs")
        handlers.registration_service.create_registration(user_id, event.id, None)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_participants", event_id=event.id).pack(),
    )

    first_page = fake_bot.sent[-1]
    assert "Страница 1/2" in first_page["text"]
    assert first_page["text"].splitlines()[-1] == "Страница 1/2"
    assert "8. [Участник 08](max://user/108) - CODE08 - Записан" in first_page["text"]
    assert "9. [Участник 09](max://user/109) - CODE09 - Записан" not in first_page["text"]
    first_buttons = {button["text"]: button for button in _buttons(first_page)}
    assert first_buttons["➡️ Далее"]["payload"] == Payload(
        "org_participants",
        event_id=event.id,
        value="1",
    ).pack()

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_participants", event_id=event.id, value="1").pack(),
    )

    second_page = fake_bot.sent[-1]
    assert "Страница 2/2" in second_page["text"]
    assert second_page["text"].splitlines()[-1] == "Страница 2/2"
    assert "9. [Участник 09](max://user/109) - CODE09 - Записан" in second_page["text"]
    assert "1. [Участник 01](max://user/101) - CODE01 - Записан" not in second_page["text"]
    second_buttons = {button["text"]: button for button in _buttons(second_page)}
    assert second_buttons["⬅️ Назад"]["payload"] == Payload(
        "org_participants",
        event_id=event.id,
        value="0",
    ).pack()


async def test_organizer_participants_button_marks_attended_and_refreshes_page(
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
        code_generator=lambda: "ATND01",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    registration = handlers.registration_service.create_registration(101, event.id, None)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload(
            "org_participant_attended",
            event_id=event.id,
            registration_id=registration.id,
            value="0",
        ).pack(),
    )

    message = fake_bot.sent[-1]
    assert storage.get_registration(registration.id).status == RegistrationStatus.ATTENDED
    assert "1. [Анна](max://user/101) - ATND01 - Пришел" in message["text"]
    assert "✅ Анна пришел" not in _button_texts(message)
    assert "↩️ Анна записан" in _button_texts(message)
    notifications = [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.ATTENDANCE_MARKED
    ]
    assert len(notifications) == 1
    assert notifications[0].user_id == 101
    assert notifications[0].registration_id == registration.id
    assert "Организатор отметил, что вы пришли" in notifications[0].message_text

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload(
            "org_participant_confirmed",
            event_id=event.id,
            registration_id=registration.id,
            value="0",
        ).pack(),
    )

    reverted = fake_bot.sent[-1]
    assert storage.get_registration(registration.id).status == RegistrationStatus.CONFIRMED
    assert "1. [Анна](max://user/101) - ATND01 - Записан" in reverted["text"]
    assert "✅ Анна пришел" in _button_texts(reverted)
    assert "↩️ Анна записан" not in _button_texts(reverted)


async def test_organizer_attendance_lookup_marks_by_code_and_keeps_state(
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
        code_generator=lambda: "123-456",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    registration = handlers.registration_service.create_registration(101, event.id, None)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_attendance_lookup", event_id=event.id).pack(),
    )

    prompt = fake_bot.sent[-1]
    state = storage.get_organizer_state(501)
    assert state is not None
    assert state.mode == "attendance_lookup"
    assert state.event_id == event.id
    assert "Отправьте код вида 123-456" in prompt["text"]
    assert "max://user/123" in prompt["text"]
    assert "⬅️ К мероприятию" in _button_texts(prompt)

    await handlers.handle_message(501, "Организатор", 9003, "123456")

    message = fake_bot.sent[-1]
    assert storage.get_registration(registration.id).status == RegistrationStatus.ATTENDED
    assert "Запись Анна отмечена как пришедшая" in message["text"]
    assert "Можно отправить следующий код или профиль" in message["text"]
    assert storage.get_organizer_state(501).mode == "attendance_lookup"
    notifications = [
        item
        for item in storage.list_notifications()
        if item.kind == NotificationKind.ATTENDANCE_MARKED
    ]
    assert len(notifications) == 1

    await handlers.handle_message(501, "Организатор", 9003, "123-456")

    assert storage.get_registration(registration.id).status == RegistrationStatus.ATTENDED
    assert len(
        [
            item
            for item in storage.list_notifications()
            if item.kind == NotificationKind.ATTENDANCE_MARKED
        ]
    ) == 1
    assert "Запись Анна отмечена как пришедшая" in fake_bot.sent[-1]["text"]


async def test_organizer_attendance_lookup_marks_by_profile_link(
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
        code_generator=lambda: "234-567",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    registration = handlers.registration_service.create_registration(101, event.id, None)

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_attendance_lookup", event_id=event.id).pack(),
    )
    await handlers.handle_message(501, "Организатор", 9003, "[Анна](max://user/101)")

    assert storage.get_registration(registration.id).status == RegistrationStatus.ATTENDED
    assert "Запись Анна отмечена как пришедшая" in fake_bot.sent[-1]["text"]


async def test_organizer_attendance_lookup_rejects_other_event_and_canceled_records(
    storage,
    fake_bot,
    fixed_now,
):
    current_event = create_event(storage, fixed_now, title="Текущий день", capacity=5)
    other_event = create_event(storage, fixed_now, title="Другой день", capacity=5)
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, current_event.id)
    storage.ensure_organizer_event(501, other_event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=iter(["111-111", "222-222", "333-333"]).__next__,
    )
    for user_id, name in [(101, "Анна"), (102, "Борис"), (103, "Сергей")]:
        handlers.registration_service.upsert_user(user_id, name)
        handlers.registration_service.record_profile_consent(user_id, "docs")
    current_registration = handlers.registration_service.create_registration(
        101,
        current_event.id,
        None,
    )
    other_registration = handlers.registration_service.create_registration(
        102,
        other_event.id,
        None,
    )
    canceled_registration = handlers.registration_service.create_registration(
        103,
        current_event.id,
        None,
    )
    storage.change_status(
        501,
        canceled_registration.id,
        RegistrationStatus.CANCELED_BY_ORGANIZER,
        now=fixed_now,
    )

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_attendance_lookup", event_id=current_event.id).pack(),
    )
    await handlers.handle_message(501, "Организатор", 9003, "222-222")

    assert storage.get_registration(current_registration.id).status == RegistrationStatus.CONFIRMED
    assert storage.get_registration(other_registration.id).status == RegistrationStatus.CONFIRMED
    assert "Запись не найдена" in fake_bot.sent[-1]["text"]
    assert storage.get_organizer_state(501).mode == "attendance_lookup"

    await handlers.handle_message(501, "Организатор", 9003, "max://user/102")

    assert storage.get_registration(other_registration.id).status == RegistrationStatus.CONFIRMED
    assert "Запись не найдена" in fake_bot.sent[-1]["text"]

    await handlers.handle_message(501, "Организатор", 9003, "333-333")

    assert (
        storage.get_registration(canceled_registration.id).status
        == RegistrationStatus.CANCELED_BY_ORGANIZER
    )
    assert "Отмененную запись нельзя отметить как пришедшую" in fake_bot.sent[-1]["text"]

    await handlers.handle_message(501, "Организатор", 9003, "https://max.ru/u/opaque")

    assert "Не нашел user_id в ссылке" in fake_bot.sent[-1]["text"]


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
    assert "🧩 Добавить слот" in _button_texts(message)
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


def _keyboard_rows(message: dict) -> list[list[dict]]:
    for attachment in message["attachments"]:
        if isinstance(attachment, dict) and attachment["type"] == "inline_keyboard":
            return attachment["payload"]["buttons"]
    raise AssertionError("inline_keyboard attachment not found")


def _has_uploaded_image(message: dict) -> bool:
    return any(
        isinstance(attachment, dict)
        and attachment.get("type") == "image"
        and isinstance(
            (attachment.get("payload") or {}).get("token"),
            str,
        )
        and bool((attachment.get("payload") or {}).get("token"))
        for attachment in message["attachments"]
    )
