from __future__ import annotations

from datetime import timedelta

import pytest

from app.bot.handlers import BotHandlers
from app.bot.payloads import Payload
from tests.conftest import create_event


def test_role_assignments_store_metadata_and_delete_organizer_access(
    storage,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Закрытая встреча")

    role = storage.ensure_role(
        502,
        "organizer",
        created_at=fixed_now,
        created_by_user_id=501,
    )
    storage.ensure_organizer_event(502, event.id)

    assert role.user_id == 502
    assert role.role == "organizer"
    assert role.created_at == fixed_now
    assert role.created_by_user_id == 501
    assert storage.get_role(502, "organizer").created_by_user_id == 501
    assert [item.user_id for item in storage.list_roles("organizer")] == [502]

    assert storage.delete_role(502, "organizer") is True

    assert storage.has_role(502, "organizer") is False
    assert storage.list_organizer_events(502, with_slots=False, with_images=False) == []


@pytest.mark.asyncio
async def test_admin_menu_is_available_only_for_admins(storage, fake_bot, fixed_now):
    storage.ensure_role(501, "admin", created_at=fixed_now)
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")
    handlers.registration_service.upsert_user(101, "Анна")
    handlers.registration_service.record_profile_consent(101, "docs")
    handlers.registration_service.upsert_user(501, "Администратор")
    handlers.registration_service.record_profile_consent(501, "docs")

    await handlers.handle_message(101, "Анна", 9001, "/start")
    assert "Меню администратора" not in _button_texts(fake_bot.sent[-1])

    await handlers.handle_callback(
        101,
        "Анна",
        9001,
        Payload("admin_menu").pack(),
    )
    assert "нет доступа" in fake_bot.sent[-1]["text"].lower()

    await handlers.handle_message(501, "Администратор", 9003, "/start")
    assert "🛠️ Меню администратора" in _button_texts(fake_bot.sent[-1])
    assert "/admin — открыть меню администратора" in fake_bot.sent[-1]["text"]

    await handlers.handle_message(501, "Администратор", 9003, "/admin")
    admin_menu = fake_bot.sent[-1]
    assert "🛠️ Меню администратора" in admin_menu["text"]
    assert _button_texts(admin_menu) == (
        "👤 Добавить Организатора 🗑️ Удалить Организатора "
        "👥 Список Организаторов 🏠 Главное меню"
    )
    _assert_admin_ui_has_no_low_contrast_or_book_emojis(admin_menu)
    assert _has_uploaded_image(admin_menu)


@pytest.mark.asyncio
async def test_admin_adds_and_removes_organizers_by_id_and_profile_links(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Встреча для Организаторов")
    storage.ensure_role(501, "admin", created_at=fixed_now)
    storage.ensure_role(901, "organizer", created_at=fixed_now)
    storage.ensure_organizer_event(601, event.id)
    handlers = BotHandlers(
        storage,
        fake_bot,
        now=lambda: fixed_now,
        organizer_config_user_ids=[901],
    )

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_add").pack(),
    )
    await handlers.handle_message(501, "Администратор", 9003, "601")

    assert storage.has_role(601, "organizer") is True
    assert storage.get_role(601, "organizer").created_by_user_id == 501
    assert "добавлен" in fake_bot.sent[-1]["text"]

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_add").pack(),
    )
    await handlers.handle_message(501, "Администратор", 9003, "[Петр](max://user/601)")

    assert "уже Организатор" in fake_bot.sent[-1]["text"]

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_remove").pack(),
    )
    await handlers.handle_message(501, "Администратор", 9003, "max://user/601")

    assert storage.has_role(601, "organizer") is False
    assert storage.list_organizer_events(601, with_slots=False, with_images=False) == []
    assert "удален" in fake_bot.sent[-1]["text"]

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_remove").pack(),
    )
    await handlers.handle_message(501, "Администратор", 9003, "https://max.ru/u/opaque")
    assert "Не нашел MAX ID" in fake_bot.sent[-1]["text"]

    await handlers.handle_message(501, "Администратор", 9003, "777")
    assert "не является Организатором" in fake_bot.sent[-1]["text"]

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_remove").pack(),
    )
    await handlers.handle_message(501, "Администратор", 9003, "901")
    assert "задан через конфиг" in fake_bot.sent[-1]["text"]
    assert storage.has_role(901, "organizer") is True


@pytest.mark.asyncio
async def test_admin_organizer_book_paginates_and_detail_can_remove(
    storage,
    fake_bot,
    fixed_now,
):
    storage.ensure_role(501, "admin", created_at=fixed_now)
    storage.upsert_user(501, "Главный администратор", now=fixed_now)
    for index in range(1, 10):
        user_id = 700 + index
        storage.upsert_user(user_id, f"Организатор {index:02d}", now=fixed_now)
        storage.ensure_role(
            user_id,
            "organizer",
            created_at=fixed_now + timedelta(minutes=index),
            created_by_user_id=501,
        )
    handlers = BotHandlers(storage, fake_bot, now=lambda: fixed_now, app_env="prod")

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_list").pack(),
    )

    first_page = fake_bot.sent[-1]
    assert "👥 Список Организаторов" in first_page["text"]
    assert "Страница 1/2" in first_page["text"]
    assert "8. [Организатор 08](max://user/708)" in first_page["text"]
    assert "9. [Организатор 09](max://user/709)" not in first_page["text"]
    assert [len(row) for row in _keyboard_rows(first_page)[:4]] == [2, 2, 2, 2]
    _assert_admin_ui_has_no_low_contrast_or_book_emojis(first_page)

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_detail", event_id=701, value="0").pack(),
    )

    detail = fake_bot.sent[-1]
    assert "🧑‍💼 Управление Организатором" in detail["text"]
    assert "[Организатор 01](max://user/701)" in detail["text"]
    assert "Кем добавлен: [Администратор](max://user/501)" in detail["text"]
    assert "🗑️ Удалить Организатора" in _button_texts(detail)
    _assert_admin_ui_has_no_low_contrast_or_book_emojis(detail)
    assert detail["format"] == "markdown"

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_remove_confirm", event_id=701, value="0").pack(),
    )
    assert "Удалить Организатора" in fake_bot.sent[-1]["text"]
    _assert_admin_ui_has_no_low_contrast_or_book_emojis(fake_bot.sent[-1])

    await handlers.handle_callback(
        501,
        "Администратор",
        9003,
        Payload("admin_org_remove_apply", event_id=701, value="0").pack(),
    )

    assert storage.has_role(701, "organizer") is False
    assert "удален" in fake_bot.sent[-1]["text"]


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


def _assert_admin_ui_has_no_low_contrast_or_book_emojis(message: dict) -> None:
    ui_text = " ".join([message["text"], _button_texts(message)])
    for emoji in ("➕", "➖", "📚"):
        assert emoji not in ui_text
