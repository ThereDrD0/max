from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.bot.handlers import BotHandlers
from app.bot.payloads import Payload
from app.domain import AccessDeniedError
from tests.conftest import FakeBotClient, create_event


async def test_first_start_shows_disclaimer_and_consent_button(
    storage, fake_bot, fixed_now
):
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now)

    await handlers.handle_bot_started(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
    )

    message = fake_bot.sent[-1]
    assert "командой хакатона" in message["text"]
    assert "не является официальной функцией платформы" in message["text"]
    assert "Согласен" in str(message["attachments"])


async def test_user_accepts_consent_and_registers_for_event(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Пробное занятие по Python")
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        code_generator=lambda: "PY2026",
    )
    await handlers.handle_bot_started(101, "Анна", 9001)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("consent_accept").pack(),
    )
    assert "Запись на мероприятия" in fake_bot.sent[-1]["text"]
    assert "📚 Мероприятия" in _button_texts(fake_bot.sent[-1])

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("register_confirm", event_id=event.id).pack(),
    )

    assert "код записи: PY2026" in fake_bot.sent[-1]["text"]
    assert "🔕 Уведомления" in str(fake_bot.sent[-1]["attachments"])
    assert "❌ Отменить" not in _button_texts(fake_bot.sent[-1])
    assert "ℹ️ Мероприятие" in _button_texts(fake_bot.sent[-1])


async def test_main_menu_contains_image_commands_and_primary_buttons(
    storage,
    fake_bot,
    fixed_now,
):
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/start")

    message = fake_bot.sent[-1]
    assert message["text"] == (
        "Запись на мероприятия\n\n"
        "Здесь можно выбрать ближайшее мероприятие, записаться и потом быстро найти свою запись. "
        "Если вы организатор, откройте отдельное меню для создания и управления мероприятиями.\n\n"
        "Команды:\n"
        "/start или /menu — открыть главное меню\n"
        "/events — показать ближайшие мероприятия\n"
        "/my или /records — показать мои записи"
    )
    image = message["attachments"][0]
    assert getattr(image, "path", "").replace("\\", "/").endswith(
        "app/assets/main-menu.png"
    )
    assert _button_texts(message) == "📚 Мероприятия 🎫 Мои записи"
    assert "Меню организатора" not in message["text"]
    assert "/organizer" not in message["text"]
    assert "/find" not in message["text"]


async def test_main_menu_shows_organizer_actions_only_for_organizer(
    storage,
    fake_bot,
    fixed_now,
):
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(501, "Организатор")
    handlers.registration_service.record_profile_consent(501, "docs")

    await handlers.handle_message(501, "Организатор", 9003, "/start")

    message = fake_bot.sent[-1]
    assert "/organizer — открыть меню организатора" in message["text"]
    assert "/find КОД — найти запись по коду, доступно организаторам" in message["text"]
    assert "🧑‍💼 Меню организатора" in _button_texts(message)


async def test_menu_command_opens_main_menu_for_user_with_consent(
    storage,
    fake_bot,
    fixed_now,
):
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/menu")

    assert "Запись на мероприятия" in fake_bot.sent[-1]["text"]
    assert "📚 Мероприятия" in _button_texts(fake_bot.sent[-1])


async def test_main_menu_buttons_open_current_sections(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Пробное занятие по Python")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_callback(101, "Анна", 9001, Payload("catalog").pack())
    assert "📚 Книга мероприятий" in fake_bot.sent[-1]["text"]
    assert "Пробное занятие по Python" in fake_bot.sent[-1]["text"]
    assert "🏠 Главное меню" in _button_texts(fake_bot.sent[-1])

    await handlers.handle_callback(101, "Анна", 9001, Payload("my_regs").pack())
    assert "🎫 У вас пока нет записей." in fake_bot.sent[-1]["text"]
    assert "🏠 Главное меню" in _button_texts(fake_bot.sent[-1])

    await handlers.handle_callback(501, "Организатор", 9003, Payload("org_menu").pack())
    assert "🧑‍💼📚 Книга мероприятий Организатора" in fake_bot.sent[-1]["text"]
    assert "Пробное занятие по Python" in fake_bot.sent[-1]["text"]
    assert "🏠 Главное меню" in _button_texts(fake_bot.sent[-1])
    assert _has_local_organizer_menu_image(fake_bot.sent[-1])


async def test_organizer_menu_paginates_and_returns_to_same_page(
    storage,
    fake_bot,
    fixed_now,
):
    events = []
    for day in range(1, 14):
        event = create_event(
            storage,
            fixed_now,
            title=f"Мероприятие {day}",
            starts_in=timedelta(days=day),
        )
        events.append(event)
        storage.ensure_organizer_event(501, event.id)
    storage.ensure_role(501, "organizer")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_message(501, "Организатор", 9003, "/organizer")

    first_page = fake_bot.sent[-1]
    assert "🧑‍💼📚 Книга мероприятий Организатора" in first_page["text"]
    assert "Страница 1/3" in first_page["text"]
    assert "🔥 БЛИЖАЙШИЕ" in first_page["text"]
    for day in range(1, 7):
        assert f"Мероприятие {day}" in first_page["text"]
    assert "Мероприятие 7" not in first_page["text"]
    assert _has_local_organizer_menu_image(first_page)

    first_buttons = _buttons(first_page)
    first_by_text = {button["text"]: button for button in first_buttons}
    manage_buttons = [
        button for button in first_buttons if button["text"].startswith("⚙️ ")
    ]
    assert len(manage_buttons) == 6
    assert "Управлять" not in _button_texts(first_page)
    first_rows = _keyboard_rows(first_page)
    assert [button["text"] for button in first_rows[0]] == [
        "⚙️ 1. Мероприятие 1",
        "⚙️ 2. Мероприятие 2",
    ]
    assert [button["text"] for button in first_rows[1]] == [
        "⚙️ 3. Мероприятие 3",
        "⚙️ 4. Мероприятие 4",
    ]
    assert [button["text"] for button in first_rows[2]] == [
        "⚙️ 5. Мероприятие 5",
        "⚙️ 6. Мероприятие 6",
    ]
    assert first_by_text["⬅️ Назад"]["payload"] == Payload("org_menu", value="2").pack()
    assert first_by_text["➡️ Далее"]["payload"] == Payload("org_menu", value="1").pack()

    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        Payload("org_menu", value="2").pack(),
    )

    last_page = fake_bot.sent[-1]
    last_buttons = _buttons(last_page)
    last_by_text = {button["text"]: button for button in last_buttons}
    assert "Страница 3/3" in last_page["text"]
    assert "Мероприятие 13" in last_page["text"]
    assert last_by_text["⬅️ Назад"]["payload"] == Payload("org_menu", value="1").pack()
    assert last_by_text["➡️ Далее"]["payload"] == Payload("org_menu", value="0").pack()

    manage_last = next(
        button
        for button in last_buttons
        if button["text"].startswith("⚙️ 13.")
    )
    assert manage_last["payload"] == Payload(
        "org_event",
        event_id=events[12].id,
        value="2",
    ).pack()

    await handlers.handle_callback(
        501,
        "Организатор",
        9003,
        manage_last["payload"],
    )

    event_menu_buttons = {button["text"]: button for button in _buttons(fake_bot.sent[-1])}
    assert event_menu_buttons["⬅️ Назад"]["payload"] == Payload(
        "org_menu",
        value="2",
    ).pack()


async def test_main_menu_callback_returns_to_role_aware_main_menu(
    storage,
    fake_bot,
    fixed_now,
):
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_callback(101, "Анна", 9001, Payload("main_menu").pack())

    assert "Запись на мероприятия" in fake_bot.sent[-1]["text"]
    assert "🧑‍💼 Меню организатора" not in _button_texts(fake_bot.sent[-1])


async def test_unknown_command_lists_available_commands(storage, fake_bot, fixed_now):
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/unknown")

    assert "Я понимаю команды:" in fake_bot.sent[-1]["text"]
    assert "/start или /menu — открыть главное меню" in fake_bot.sent[-1]["text"]
    assert "/organizer" not in fake_bot.sent[-1]["text"]
    assert "/find" not in fake_bot.sent[-1]["text"]


async def test_callback_edits_source_message_instead_of_adding_new_one(
    storage, fake_bot, fixed_now
):
    create_event(storage, fixed_now, title="Пробное занятие по Python")
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        max_bot_username="id123_bot",
    )
    handlers.registration_service.upsert_user(101, "Анна")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("consent_accept").pack(),
        source_message_id="mid.menu",
    )

    assert fake_bot.sent == []
    assert fake_bot.edited[-1]["message_id"] == "mid.menu"
    assert "Запись на мероприятия" in fake_bot.edited[-1]["text"]
    assert "📚 Мероприятия" in _button_texts(fake_bot.edited[-1])


async def test_text_command_deletes_source_user_message_after_reply(
    storage, fake_bot, fixed_now
):
    create_event(storage, fixed_now, title="Пробное занятие по Python")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(
        101,
        "Анна",
        9001,
        "/events",
        source_message_id="mid.user-command",
    )

    assert "Пробное занятие по Python" in fake_bot.sent[-1]["text"]
    assert fake_bot.deleted == ["mid.user-command"]


async def test_catalog_hides_raw_ids_in_prod_and_keeps_buttons_short(
    storage, fake_bot, fixed_now
):
    event = create_event(
        storage,
        fixed_now,
        title="Очень длинное название мероприятия, которое не должно попадать в кнопку",
    )
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/events")

    message = fake_bot.sent[-1]
    assert f"[DEV] event_id={event.id}" not in message["text"]
    buttons = _keyboard_rows(message)
    flattened = [button["text"] for row in buttons for button in row]
    assert "Очень длинное название" not in " ".join(flattened)
    assert any(text.startswith("1. ") for text in flattened)
    assert "Подробнее" not in " ".join(flattened)
    assert not any("📝" in text for text in flattened)


async def test_catalog_hides_full_events_and_omits_place_status(
    storage,
    fake_bot,
    fixed_now,
):
    available = create_event(storage, fixed_now, title="Доступное мероприятие")
    full = create_event(storage, fixed_now, title="Заполненное мероприятие", capacity=1)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(202, "Борис")
    handlers.registration_service.record_profile_consent(202, "docs")
    handlers.registration_service.create_registration(202, full.id, None)
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/events")

    message = fake_bot.sent[-1]
    assert "Доступное мероприятие" in message["text"]
    assert "Заполненное мероприятие" not in message["text"]
    assert "есть места" not in message["text"]
    assert "мест нет" not in message["text"]
    assert "🕒 90 мин. · очно" in message["text"]
    assert "⏱" not in message["text"]
    assert len(
        [button for button in _buttons(message) if button["text"].startswith("1. ")]
    ) == 1


async def test_catalog_opens_event_detail_before_booking(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="День открытых дверей ИТ-института")
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        max_bot_username="id123_bot",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/events")

    catalog_buttons = _keyboard_rows(fake_bot.sent[-1])
    catalog_texts = [button["text"] for row in catalog_buttons for button in row]
    assert "1. День открытых дверей..." in catalog_texts
    assert all("Подробнее" not in text for text in catalog_texts)
    assert all("📝" not in text for text in catalog_texts)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
        source_message_id="mid.catalog",
    )

    detail = fake_bot.edited[-1]
    assert "Встреча с кафедрой" in detail["text"]
    detail_buttons = detail["attachments"][0]["payload"]["buttons"]
    detail_flat_buttons = [button for row in detail_buttons for button in row]
    detail_texts = [button["text"] for button in detail_flat_buttons]
    assert "📝 Записаться" in detail_texts
    assert "🔗 Поделиться" in detail_texts
    assert "⬅️ К каталогу" in detail_texts
    assert any(
        button.get("type") == "clipboard"
        and button.get("payload", "").startswith("https://max.ru/id123_bot?start=e_")
        for button in detail_flat_buttons
    )


async def test_catalog_book_first_page_highlights_soon_events_without_duplicates(
    storage,
    fake_bot,
    fixed_now,
):
    for day in range(1, 8):
        create_event(
            storage,
            fixed_now,
            title=f"Событие {day}",
            starts_in=timedelta(days=day),
        )
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/events")

    message = fake_bot.sent[-1]
    assert "📚 Книга мероприятий" in message["text"]
    assert "Страница 1/2" in message["text"]
    assert "Листайте книгу кнопками ниже" in message["text"]
    assert "🔥 УЖЕ СКОРО" in message["text"]
    for day in range(1, 7):
        assert message["text"].count(f"Событие {day}") == 1
    assert "Событие 7" not in message["text"]
    soon_text = message["text"].split("🔥 УЖЕ СКОРО", 1)[1].split("Событие 4", 1)[0]
    assert soon_text.count("Событие ") == 3

    buttons = _buttons(message)
    button_texts = [button["text"] for button in buttons]
    detail_buttons = [
        text for text in button_texts if text[:1].isdigit() and text[1:3] == ". "
    ]
    assert len(detail_buttons) == 6
    rows = _keyboard_rows(message)
    assert [button["text"] for button in rows[0]] == [
        "1. Событие 1",
        "2. Событие 2",
    ]
    assert [button["text"] for button in rows[1]] == [
        "3. Событие 3",
        "4. Событие 4",
    ]
    assert [button["text"] for button in rows[2]] == [
        "5. Событие 5",
        "6. Событие 6",
    ]
    assert "Подробнее" not in " ".join(button_texts)
    assert "🎫 Мои записи" not in button_texts
    assert "⬅️ Назад" in button_texts
    assert "➡️ Далее" in button_texts
    assert "🏠 Главное меню" in button_texts
    assert _has_local_main_menu_image(message)


async def test_catalog_navigation_wraps_and_keeps_image_on_later_pages(
    storage,
    fake_bot,
    fixed_now,
):
    for day in range(1, 14):
        create_event(
            storage,
            fixed_now,
            title=f"Событие {day}",
            starts_in=timedelta(days=day),
        )
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/events")

    first_buttons = _buttons(fake_bot.sent[-1])
    first_by_text = {button["text"]: button for button in first_buttons}
    assert "Страница 1/3" in fake_bot.sent[-1]["text"]
    assert first_by_text["⬅️ Назад"]["payload"] == Payload("catalog", value="2").pack()
    assert first_by_text["➡️ Далее"]["payload"] == Payload("catalog", value="1").pack()

    await handlers.handle_callback(101, "Анна", 9001, Payload("catalog", value="2").pack())

    last_page = fake_bot.sent[-1]
    last_by_text = {button["text"]: button for button in _buttons(last_page)}
    assert "Страница 3/3" in last_page["text"]
    assert "Листайте книгу кнопками ниже" not in last_page["text"]
    assert "🔥 УЖЕ СКОРО" not in last_page["text"]
    assert "Событие 13" in last_page["text"]
    assert last_by_text["⬅️ Назад"]["payload"] == Payload("catalog", value="1").pack()
    assert last_by_text["➡️ Далее"]["payload"] == Payload("catalog", value="0").pack()
    assert _has_local_main_menu_image(last_page)


async def test_event_detail_includes_event_image_before_keyboard(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="День открытых дверей ИТ-института")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    storage.set_event_image(
        501,
        event.id,
        token="image-token",
        url="https://max.example/image.png",
        now=fixed_now,
    )
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )

    detail = fake_bot.sent[-1]
    assert detail["attachments"][0] == {
        "type": "image",
        "payload": {"token": "image-token"},
    }
    assert detail["attachments"][-1]["type"] == "inline_keyboard"


async def test_organizer_sets_event_image_from_next_photo_message(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Пробное занятие по Python")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        user_id=501,
        display_name="Организатор",
        chat_id=9003,
        payload=Payload("org_image", event_id=event.id).pack(),
    )

    assert storage.get_pending_event_image(501) == event.id
    assert "Отправьте картинку одним сообщением" in fake_bot.sent[-1]["text"]

    await handlers.handle_message(
        501,
        "Организатор",
        9003,
        "",
        attachments=[
            {
                "type": "image",
                "payload": {
                    "token": "image-token",
                    "url": "https://max.example/image.png",
                },
            }
        ],
    )

    updated_event = storage.get_event(event.id)
    assert updated_event is not None
    assert updated_event.image_token == "image-token"
    assert updated_event.image_url == "https://max.example/image.png"
    assert storage.get_pending_event_image(501) is None
    assert "Картинка обновлена" in fake_bot.sent[-1]["text"]


async def test_pending_event_image_keeps_waiting_when_message_has_no_image(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Пробное занятие по Python")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    storage.set_pending_event_image(501, event.id, now=fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_message(
        501,
        "Организатор",
        9003,
        "не картинка",
        attachments=[],
    )

    assert "Жду картинку" in fake_bot.sent[-1]["text"]
    assert storage.get_pending_event_image(501) == event.id
    assert storage.get_event(event.id).image_token is None


def test_organizer_cannot_set_event_image_without_event_access(
    storage, fixed_now
):
    event = create_event(storage, fixed_now, title="Закрытое мероприятие")
    storage.ensure_role(501, "organizer")

    with pytest.raises(AccessDeniedError):
        storage.set_event_image(
            501,
            event.id,
            token="image-token",
            url="https://max.example/image.png",
            now=fixed_now,
        )

    stored_event = storage.get_event(event.id)
    assert stored_event is not None
    assert stored_event.image_token is None


async def test_my_registrations_hides_cancelled_record_after_rebooking_same_event(
    storage, fake_bot, fixed_now
):
    current_now = fixed_now
    event = create_event(storage, fixed_now, title="День открытых дверей ИТ-института")
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: current_now,
        app_env="prod",
        code_generator=iter(["OLD111", "NEW222"]).__next__,
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    old_registration = handlers.registration_service.create_registration(101, event.id, None)
    current_now = fixed_now + timedelta(minutes=1)
    handlers.registration_service.cancel_registration(101, old_registration.id)
    current_now = fixed_now + timedelta(minutes=2)
    handlers.registration_service.create_registration(101, event.id, None)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("my_regs").pack(),
    )

    message = fake_bot.sent[-1]
    assert "Код: NEW222" in message["text"]
    assert "Код: OLD111" not in message["text"]
    assert "Отменена пользователем" not in message["text"]


async def test_my_registrations_links_to_event_detail_and_marks_status_visually(
    storage, fake_bot, fixed_now
):
    active_event = create_event(storage, fixed_now, title="День открытых дверей ИТ-института")
    canceled_event = create_event(storage, fixed_now, title="Онлайн-консультация по поступлению")
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=iter(["OPEN01", "CLOSE1"]).__next__,
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.create_registration(101, active_event.id, None)
    canceled_registration = handlers.registration_service.create_registration(101, canceled_event.id, None)
    handlers.registration_service.cancel_registration(101, canceled_registration.id)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("my_regs").pack(),
    )

    message = fake_bot.sent[-1]
    buttons = _buttons(message)
    assert "Статус: ✅ Подтверждена" in message["text"]
    assert "Статус: ⚪ Отменена пользователем" in message["text"]
    assert "✅ 1 ℹ️ День открытых дверей..." in _button_texts(message)
    assert "⚪ 2 ℹ️ Онлайн-консультация..." in _button_texts(message)
    active_button = next(button for button in buttons if button["text"].startswith("✅ 1"))
    canceled_button = next(button for button in buttons if button["text"].startswith("⚪ 2"))
    assert active_button["payload"] == Payload("event_detail", event_id=active_event.id).pack()
    assert active_button["intent"] == "positive"
    assert canceled_button["payload"] == Payload("event_detail", event_id=canceled_event.id).pack()
    assert canceled_button["intent"] == "default"
    assert not any(button["payload"].startswith("reg_cancel") for button in buttons)


async def test_closed_registration_hides_catalog_but_keeps_user_record_visible(
    storage,
    fake_bot,
    fixed_now,
):
    open_event = create_event(storage, fixed_now, title="Открытая экскурсия")
    closed_event = create_event(storage, fixed_now, title="Закрытая встреча")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, closed_event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "KEEP01",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.create_registration(101, closed_event.id, None)
    handlers.organizer_service.close_registration(501, closed_event.id)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("catalog").pack(),
    )

    catalog = fake_bot.sent[-1]
    assert "Открытая экскурсия" in catalog["text"]
    assert "Закрытая встреча" not in catalog["text"]

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("my_regs").pack(),
    )

    records = fake_bot.sent[-1]
    assert "Закрытая встреча" in records["text"]
    assert "Код: KEEP01" in records["text"]
    assert (
        "Регистрация новых участников закрыта. "
        "Ваша запись действует: вы участвуете в мероприятии."
    ) in records["text"]

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=closed_event.id).pack(),
    )

    detail = fake_bot.sent[-1]
    assert "✅ ВЫ УЖЕ ЗАПИСАНЫ НА ЭТО МЕРОПРИЯТИЕ." in detail["text"]
    assert (
        "Регистрация новых участников закрыта. "
        "Ваша запись действует: вы участвуете в мероприятии."
    ) in detail["text"]
    assert "📝 Записаться" not in _button_texts(detail)


async def test_event_detail_cancel_requires_explicit_confirmation(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="День открытых дверей ИТ-института")
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "OPEN01",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    registration = handlers.registration_service.create_registration(101, event.id, None)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )

    detail = fake_bot.sent[-1]
    assert "❌ Отменить запись" in _button_texts(detail)
    cancel_button = next(
        button for button in _buttons(detail) if button["text"] == "❌ Отменить запись"
    )
    assert cancel_button["payload"] == Payload(
        "reg_cancel_confirm",
        event_id=event.id,
        registration_id=registration.id,
    ).pack()

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=cancel_button["payload"],
    )

    confirmation = fake_bot.sent[-1]
    assert "⚠️ ОТМЕНА ЗАПИСИ" in confirmation["text"]
    assert "Вы отменяете запись на мероприятие:" in confirmation["text"]
    assert "**" not in confirmation["text"]
    assert "День открытых дверей ИТ-института" in confirmation["text"]
    assert "Код записи: OPEN01" in confirmation["text"]
    assert "✅ Оставить запись" in _button_texts(confirmation)
    assert "❌ Да, отменить запись" in _button_texts(confirmation)
    assert next(
        button for button in _buttons(confirmation) if button["text"] == "✅ Оставить запись"
    )["intent"] == "positive"
    assert next(
        button for button in _buttons(confirmation) if button["text"] == "❌ Да, отменить запись"
    )["intent"] == "negative"
    assert storage.get_registration(registration.id).status.value == "confirmed"

    confirm_button = next(
        button for button in _buttons(confirmation) if button["text"] == "❌ Да, отменить запись"
    )
    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=confirm_button["payload"],
    )

    assert "Запись отменена." in fake_bot.sent[-1]["text"]
    assert storage.get_registration(registration.id).status.value == "canceled_by_user"


async def test_event_detail_refreshes_available_places_after_other_registration(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Пробное занятие", capacity=2)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.upsert_user(202, "Борис")
    handlers.registration_service.record_profile_consent(202, "docs")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )
    handlers.registration_service.create_registration(202, event.id, None)
    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )

    assert "✅ Свободных мест: 2 из 2" in fake_bot.sent[-2]["text"]
    assert "✅ Свободных мест: 1 из 2" in fake_bot.sent[-1]["text"]


async def test_event_detail_hides_booking_when_all_slots_are_full(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Экскурсия", with_slots=True)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=iter(["SLOT01", "SLOT02"]).__next__,
    )
    for user_id in [101, 201, 202]:
        handlers.registration_service.upsert_user(user_id, f"User {user_id}")
        handlers.registration_service.record_profile_consent(user_id, "docs")
    handlers.registration_service.create_registration(201, event.id, event.slots[0].id)
    handlers.registration_service.create_registration(202, event.id, event.slots[1].id)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )

    message = fake_bot.sent[-1]
    assert "✅ Свободных мест: 0 из 2" in message["text"]
    assert "Свободных мест нет." in message["text"]
    assert "📝 Записаться" not in _button_texts(message)


async def test_event_detail_shows_slot_capacity_as_current_of_maximum(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Экскурсия", with_slots=True)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "SLOT01",
    )
    for user_id in [101, 201]:
        handlers.registration_service.upsert_user(user_id, f"User {user_id}")
        handlers.registration_service.record_profile_consent(user_id, "docs")
    handlers.registration_service.create_registration(201, event.id, event.slots[0].id)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_detail", event_id=event.id).pack(),
    )

    assert "✅ Свободных мест: 1 из 2" in fake_bot.sent[-1]["text"]


async def test_stale_booking_button_rechecks_available_places(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Пробное занятие", capacity=1)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        app_env="prod",
        code_generator=lambda: "TAKEN1",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.upsert_user(202, "Борис")
    handlers.registration_service.record_profile_consent(202, "docs")
    handlers.registration_service.create_registration(202, event.id, None)

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_book", event_id=event.id).pack(),
    )

    assert fake_bot.sent[-1]["text"] == "Свободных мест уже нет."


async def test_stale_booking_button_rechecks_event_start_time(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Пробное занятие")
    storage.update_event_start(event.id, fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_callback(
        user_id=101,
        display_name="Анна",
        chat_id=9001,
        payload=Payload("event_book", event_id=event.id).pack(),
    )

    assert fake_bot.sent[-1]["text"] == "Регистрация закрыта."


async def test_stale_booking_buttons_recheck_closed_registration(
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
        code_generator=lambda: "CLOSED",
    )
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.organizer_service.close_registration(501, event.id)

    stale_payloads = [
        Payload("event_book", event_id=event.id),
        Payload("slot_pick", event_id=event.id, slot_id=event.slots[0].id),
        Payload("register_confirm", event_id=event.id, slot_id=event.slots[0].id),
    ]

    for payload in stale_payloads:
        await handlers.handle_callback(
            user_id=101,
            display_name="Анна",
            chat_id=9001,
            payload=payload.pack(),
        )
        assert fake_bot.sent[-1]["text"] == "Регистрация закрыта."

    assert handlers.registration_service.list_user_registrations(101) == []


async def test_repeated_start_deletes_previous_bot_message(
    storage, fake_bot, fixed_now
):
    create_event(storage, fixed_now, title="Пробное занятие по Python")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/start")
    await handlers.handle_message(101, "Анна", 9001, "/start")

    assert fake_bot.deleted == ["mid.1"]


async def test_concurrent_menu_aliases_leave_only_one_visible_bot_message(
    storage,
    fixed_now,
):
    class SlowBotClient(FakeBotClient):
        async def send_message(self, **kwargs):
            await asyncio.sleep(0.01)
            return await super().send_message(**kwargs)

    slow_bot = SlowBotClient()
    storage.upsert_user(101, "Анна", now=fixed_now)
    storage.record_profile_consent(101, "docs", now=fixed_now)
    start_handlers = BotHandlers(storage, slow_bot, now=lambda: fixed_now, app_env="prod")
    menu_handlers = BotHandlers(storage, slow_bot, now=lambda: fixed_now, app_env="prod")

    await asyncio.gather(
        start_handlers.handle_message(101, "Анна", 9001, "/start"),
        menu_handlers.handle_message(101, "Анна", 9001, "/menu"),
    )

    assert len(slow_bot.sent) == 2
    assert slow_bot.deleted == ["mid.1"]
    assert storage.get_last_bot_message_id(101) == "mid.2"


async def test_catalog_shows_dev_ids_only_in_dev_mode(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="Пробное занятие по Python")
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="local")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/events")

    assert f"[DEV] event_id={event.id}" in fake_bot.sent[-1]["text"]


async def test_organizer_menu_is_available_only_for_role(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Пробное занятие по Python")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now)

    await handlers.handle_message(777, "Петр", 9002, "/organizer")
    assert "нет доступа" in fake_bot.sent[-1]["text"].lower()

    await handlers.handle_message(501, "Организатор", 9003, "/organizer")
    assert "Пробное занятие по Python" in fake_bot.sent[-1]["text"]


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


def _has_local_main_menu_image(message: dict) -> bool:
    return any(
        getattr(attachment, "path", "").replace("\\", "/").endswith(
            "app/assets/main-menu.png"
        )
        for attachment in message["attachments"]
    )


def _has_local_organizer_menu_image(message: dict) -> bool:
    return any(
        getattr(attachment, "path", "").replace("\\", "/").endswith(
            "app/assets/organizer-menu.png"
        )
        for attachment in message["attachments"]
    )
