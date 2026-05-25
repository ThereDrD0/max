from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from threading import Lock
from zoneinfo import ZoneInfo

from app.bot.assets import BotImageAsset, image_attachment
from app.bot.client import BotClient
from app.bot.deeplinks import (
    MAX_START_PAYLOAD_LIMIT,
    EVENT_PAYLOAD_PREFIX,
    build_default_event_slug,
    build_event_deeplink,
    parse_start_payload,
)
from app.bot.keyboards import (
    callback_button,
    clipboard_button,
    consent_keyboard,
    inline_keyboard,
)
from app.bot.organizer_datetime import (
    combine_moscow_datetime,
    parse_organizer_date,
    parse_organizer_time,
)
from app.bot.payloads import Payload
from app.domain import (
    AccessDeniedError,
    AttendanceMarkDeniedError,
    BotDomainError,
    ConsentRequiredError,
    DuplicateActiveRegistrationError,
    DuplicateEventSlugError,
    EventStartInPastError,
    LateCancellationDeniedError,
    NoSeatsAvailableError,
    RegistrationClosedError,
    RegistrationNotFoundError,
    SlotNotFoundError,
)
from app.enums import (
    ACTIVE_REGISTRATION_STATUSES,
    EventFormat,
    LateCancelPolicy,
    NotificationKind,
    RegistrationStatus,
)
from app.services.event_cleanup import ORGANIZER_EVENT_RETENTION_DAYS
from app.services.organizer import OrganizerService
from app.services.registration import CodeGenerator, RegistrationService
from app.services.registration_codes import (
    extract_max_user_id,
    normalize_registration_code_input,
)
from app.observability.performance import measure
from app.storage.base import Storage
from app.storage.entities import Event, EventSlot, OrganizerState, Registration


DISCLAIMER_TEXT = (
    "👋 Здравствуйте. Это сервис записи на мероприятия университета.\n\n"
    "Сервис разработан командой хакатона университета и не является "
    "официальной функцией платформы MAX. Для работы мы храним только "
    "идентификатор пользователя MAX, отображаемое имя, выбранное мероприятие "
    "и статус записи. Телефон, паспортные и банковские данные не запрашиваются."
)
MAIN_MENU_HEADER_TEXT = (
    "Запись на мероприятия\n\n"
    "Здесь можно выбрать ближайшее мероприятие, записаться и потом быстро найти свою запись. "
    "Если вы организатор, откройте отдельное меню для создания и управления мероприятиями.\n\n"
    "Команды:"
)
BASE_COMMAND_LINES = (
    "/start или /menu — открыть главное меню",
    "/events — показать ближайшие мероприятия",
    "/my или /records — показать мои записи",
)
ORGANIZER_COMMAND_LINES = (
    "/organizer — открыть меню организатора",
    "/find КОД — найти запись по коду, доступно организаторам",
)
REGISTRATION_CLOSED_ACTIVE_RECORD_TEXT = (
    "Регистрация новых участников закрыта. "
    "Ваша запись действует: вы участвуете в мероприятии."
)

CATALOG_PAGE_SIZE = 6
ORGANIZER_BOOK_PAGE_SIZE = 6
PARTICIPANTS_BOOK_PAGE_SIZE = 8
ORGANIZER_BOOK_BUTTON_TITLE_MAX_CHARS = 30
PARTICIPANT_BUTTON_NAME_MAX_CHARS = 24
CATALOG_SOON_COUNT = 3
_SEND_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
_SEND_LOCKS_GUARD = Lock()
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
BUILDER_MODE_CREATE = "builder_new"
BUILDER_MODE_EDIT = "builder_edit"
STATE_EDIT_DATE = "edit_date"
STATE_EDIT_TIME = "edit_time"
STATE_EDIT_PLACE = "edit_place"
STATE_MANUAL_REMINDER_TEXT = "manual_reminder_text"
STATE_ATTENDANCE_LOOKUP = "attendance_lookup"
CREATE_EVENT_BUTTON_TEXT = "📝 Создать мероприятие"
TAKE_CURRENT_TEXT = "♻️ ВЗЯТЬ ТЕКУЩЕЕ"


def _send_lock_for(user_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), user_id)
    with _SEND_LOCKS_GUARD:
        lock = _SEND_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SEND_LOCKS[key] = lock
        return lock


class BotHandlers:
    def __init__(
        self,
        storage: Storage,
        bot_client: BotClient,
        *,
        now: Callable[[], datetime] | None = None,
        code_generator: CodeGenerator | None = None,
        documents_version: str = "hackathon-2026-05",
        app_env: str = "local",
        max_bot_username: str = "",
    ) -> None:
        self.storage = storage
        self.bot = bot_client
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.documents_version = documents_version
        self.dev_mode = app_env.lower() in {"dev", "development", "local", "test"}
        self.max_bot_username = max_bot_username
        self._source_message_id: str | None = None
        self._source_user_message_id: str | None = None
        self.registration_service = RegistrationService(
            storage,
            now=self.now,
            code_generator=code_generator,
        )
        self.organizer_service = OrganizerService(storage, now=self.now)

    async def handle_bot_started(
        self,
        user_id: int,
        display_name: str,
        chat_id: int | None,
        start_payload: str | None = None,
    ) -> None:
        self.registration_service.upsert_user(user_id, display_name)
        if start_payload:
            await self._send_deeplink_entrypoint(user_id, chat_id, start_payload)
            return
        await self._send_entrypoint(user_id, chat_id)

    async def handle_message(
        self,
        user_id: int,
        display_name: str,
        chat_id: int | None,
        text: str,
        source_message_id: str | None = None,
        attachments: list | None = None,
    ) -> None:
        self.registration_service.upsert_user(user_id, display_name)
        self._source_user_message_id = source_message_id
        try:
            normalized = (text or "").strip()
            if normalized == "/organizer":
                self.storage.clear_organizer_state(user_id)
                self.storage.clear_pending_event_image(user_id)
                await self._send_organizer_menu(user_id, chat_id)
                return
            organizer_state = self.storage.get_organizer_state(user_id)
            if organizer_state is not None:
                await self._handle_organizer_state_message(
                    user_id,
                    chat_id,
                    organizer_state,
                    normalized,
                    attachments,
                )
                return
            pending_event_id = self.storage.get_pending_event_image(user_id)
            if pending_event_id is not None:
                await self._handle_pending_event_image(
                    user_id,
                    chat_id,
                    pending_event_id,
                    attachments,
                )
                return
            if normalized in {"/start", "/menu", ""}:
                await self._send_entrypoint(user_id, chat_id)
                return
            if normalized in {"/events", "мероприятия"}:
                await self._send_catalog(user_id, chat_id)
                return
            if normalized in {"/my", "/records", "мои записи"}:
                await self._send_my_registrations(user_id, chat_id)
                return
            if normalized.startswith("/find "):
                await self._send_find_result(user_id, chat_id, normalized[6:].strip())
                return
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=self._unknown_command_text(user_id),
            )
        finally:
            self._source_user_message_id = None

    async def handle_callback(
        self,
        user_id: int,
        display_name: str,
        chat_id: int | None,
        payload: str,
        source_message_id: str | None = None,
    ) -> None:
        self.registration_service.upsert_user(user_id, display_name)
        data = Payload.unpack(payload)
        self._source_message_id = source_message_id
        try:
            await self._dispatch_callback(user_id, chat_id, data)
        except BotDomainError as exc:
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=self._friendly_error(exc),
            )
        finally:
            self._source_message_id = None

    async def _dispatch_callback(
        self,
        user_id: int,
        chat_id: int | None,
        data: Payload,
    ) -> None:
        if data.action == "consent_accept":
            self.registration_service.record_profile_consent(
                user_id,
                self.documents_version,
            )
            await self._send_main_menu(user_id, chat_id)
        elif data.action == "consent_accept_event" and data.value:
            self.registration_service.record_profile_consent(
                user_id,
                self.documents_version,
            )
            event = self.storage.get_event_by_slug(data.value)
            if event is None:
                await self._send_invalid_deeplink_entrypoint(user_id, chat_id)
                return
            await self._send_event_detail(user_id, chat_id, event.id)
        elif data.action == "main_menu":
            self.storage.clear_organizer_state(user_id)
            self.storage.clear_pending_event_image(user_id)
            await self._send_main_menu(user_id, chat_id)
        elif data.action == "catalog":
            await self._send_catalog(user_id, chat_id, page=self._payload_page(data))
        elif data.action == "event_detail" and data.event_id is not None:
            await self._send_event_detail(
                user_id,
                chat_id,
                data.event_id,
                page=self._payload_page(data),
            )
        elif data.action == "event_book" and data.event_id is not None:
            await self._send_booking_step(user_id, chat_id, data.event_id)
        elif data.action == "slot_pick" and data.event_id is not None:
            await self._send_registration_summary(
                user_id,
                chat_id,
                data.event_id,
                data.slot_id,
            )
        elif data.action == "register_confirm" and data.event_id is not None:
            registration = self.registration_service.create_registration(
                user_id,
                data.event_id,
                data.slot_id,
            )
            await self._send_registration_success(user_id, chat_id, registration)
        elif data.action == "my_regs":
            await self._send_my_registrations(user_id, chat_id)
        elif data.action in {"reg_cancel", "reg_cancel_confirm"} and data.registration_id is not None:
            await self._send_cancel_confirmation(user_id, chat_id, data.registration_id)
        elif data.action == "reg_cancel_apply" and data.registration_id is not None:
            registration = self.registration_service.cancel_registration(
                user_id,
                data.registration_id,
            )
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    "❌ Запись отменена. Место снова доступно другим абитуриентам.\n"
                    f"Код записи: {registration.code}"
                ),
                attachments=inline_keyboard(
                    [[callback_button("📚 Выбрать другое мероприятие", Payload("catalog"))]]
                ),
            )
        elif data.action == "notif_toggle" and data.registration_id is not None:
            enabled = data.value == "on"
            self.registration_service.set_notifications_enabled(
                user_id,
                data.registration_id,
                enabled=enabled,
            )
            text = "🔔 Уведомления по этой записи включены." if enabled else "🔕 Уведомления по этой записи отключены."
            await self._send(user_id=user_id, chat_id=chat_id, text=text)
        elif data.action == "org_menu":
            self.storage.clear_organizer_state(user_id)
            await self._send_organizer_menu(
                user_id,
                chat_id,
                page=self._payload_page(data),
            )
        elif data.action == "org_event" and data.event_id is not None:
            self.storage.clear_organizer_state(user_id)
            await self._send_organizer_event(
                user_id,
                chat_id,
                data.event_id,
                page=self._payload_page(data),
            )
        elif data.action == "org_participants" and data.event_id is not None:
            self.storage.clear_organizer_state(user_id)
            await self._send_organizer_participants(
                user_id,
                chat_id,
                data.event_id,
                page=self._payload_page(data),
            )
        elif data.action == "org_attendance_lookup" and data.event_id is not None:
            await self._start_attendance_lookup(
                user_id,
                chat_id,
                data.event_id,
                page=self._payload_page(data),
            )
        elif data.action == "org_create":
            await self._start_event_builder(user_id, chat_id, mode=BUILDER_MODE_CREATE)
        elif data.action == "org_rebuild" and data.event_id is not None:
            await self._start_event_builder(
                user_id,
                chat_id,
                mode=BUILDER_MODE_EDIT,
                event_id=data.event_id,
            )
        elif data.action == "org_datetime" and data.event_id is not None:
            await self._send_organizer_datetime_menu(user_id, chat_id, data.event_id)
        elif data.action == "org_edit_date" and data.event_id is not None:
            await self._start_simple_organizer_state(
                user_id,
                chat_id,
                STATE_EDIT_DATE,
                data.event_id,
                "Отправьте новую дату мероприятия.",
            )
        elif data.action == "org_edit_time" and data.event_id is not None:
            await self._start_simple_organizer_state(
                user_id,
                chat_id,
                STATE_EDIT_TIME,
                data.event_id,
                "Отправьте новое время мероприятия.",
            )
        elif data.action == "org_place" and data.event_id is not None:
            await self._start_simple_organizer_state(
                user_id,
                chat_id,
                STATE_EDIT_PLACE,
                data.event_id,
                "Отправьте новое место или ссылку одним сообщением.",
            )
        elif data.action == "org_remind" and data.event_id is not None:
            await self._send_organizer_reminder_entry(user_id, chat_id, data.event_id)
        elif data.action == "org_close_confirm" and data.event_id is not None:
            await self._send_organizer_close_confirmation(user_id, chat_id, data.event_id)
        elif data.action == "org_event_close_confirm" and data.event_id is not None:
            await self._send_organizer_event_close_confirmation(user_id, chat_id, data.event_id)
        elif data.action == "org_remind_all" and data.event_id is not None:
            await self._start_manual_reminder_text(
                user_id,
                chat_id,
                data.event_id,
                slot_id=None,
            )
        elif data.action == "org_remind_slot" and data.event_id is not None:
            await self._start_manual_reminder_text(
                user_id,
                chat_id,
                data.event_id,
                slot_id=data.slot_id,
            )
        elif data.action == "org_remind_auto":
            await self._finish_manual_reminder_text(user_id, chat_id, custom_text=None)
        elif data.action in {
            "org_builder_current",
            "org_builder_format",
            "org_builder_no_slots",
            "org_builder_slots_add",
            "org_builder_slots_done",
            "org_builder_skip_image",
            "org_builder_cancel",
            "org_builder_cancel_confirm",
            "org_builder_cancel_back",
        }:
            await self._handle_builder_callback(user_id, chat_id, data)
        elif data.action == "org_image" and data.event_id is not None:
            self.storage.set_pending_event_image(user_id, data.event_id, now=self.now())
            event = self.storage.get_event(
                data.event_id,
                with_slots=False,
                with_image=False,
            )
            event_title = event.title if event else "мероприятия"
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    "Отправьте картинку одним сообщением. "
                    f"Она станет обложкой мероприятия «{event_title}»."
                ),
            )
        elif data.action == "org_close" and data.event_id is not None:
            event = self.organizer_service.close_registration(user_id, data.event_id)
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    f"Регистрация на «{event.title}» закрыта.\n\n"
                    "Новые участники больше не увидят мероприятие в каталоге "
                    "и не смогут записаться. Текущие записи остаются действующими: "
                    "участники остаются на мероприятии и получат уведомления."
                ),
                attachments=inline_keyboard(
                    [
                        [
                            callback_button(
                                "ℹ️ Мероприятие",
                                Payload("org_event", event_id=event.id),
                            )
                        ],
                        [callback_button("⬅️ Назад", Payload("org_menu"))],
                    ]
                ),
            )
        elif data.action == "org_event_close" and data.event_id is not None:
            result = self.organizer_service.close_event(user_id, data.event_id)
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    f"Мероприятие «{result.event.title}» закрыто.\n\n"
                    "Все активные записи отменены. "
                    f"Уведомления поставлены в очередь для {result.notification_count} участников."
                ),
                attachments=inline_keyboard(
                    [
                        [
                            callback_button(
                                "ℹ️ Мероприятие",
                                Payload("org_event", event_id=result.event.id),
                            )
                        ],
                        [callback_button("⬅️ Назад", Payload("org_menu"))],
                    ]
                ),
            )
        elif data.action == "org_notify" and data.event_id is not None and data.value:
            created = self.organizer_service.enqueue_manual_notification(
                user_id,
                data.event_id,
                NotificationKind(data.value),
            )
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=f"Уведомление поставлено в очередь для {len(created)} участников.",
            )
        elif data.action == "org_attended" and data.registration_id is not None:
            registration = self.organizer_service.mark_attended_with_notification(
                user_id,
                data.registration_id,
            )
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=f"Посещение отмечено для записи {registration.code}.",
            )
        elif (
            data.action == "org_participant_attended"
            and data.event_id is not None
            and data.registration_id is not None
        ):
            registration = self.organizer_service.mark_attended_with_notification(
                user_id,
                data.registration_id,
            )
            await self._send_organizer_participants(
                user_id,
                chat_id,
                registration.event_id,
                page=self._payload_page(data),
            )
        elif (
            data.action == "org_participant_confirmed"
            and data.event_id is not None
            and data.registration_id is not None
        ):
            registration = self.organizer_service.mark_confirmed(
                user_id,
                data.registration_id,
            )
            await self._send_organizer_participants(
                user_id,
                chat_id,
                registration.event_id,
                page=self._payload_page(data),
            )
        else:
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text="Не понял действие. Откройте меню заново через /start.",
            )

    async def _send_deeplink_entrypoint(
        self,
        user_id: int,
        chat_id: int | None,
        start_payload: str,
    ) -> None:
        slug = parse_start_payload(start_payload)
        if slug is None:
            await self._send_invalid_deeplink_entrypoint(user_id, chat_id)
            return
        event = self.storage.get_event_by_slug(slug)
        if event is None:
            await self._send_invalid_deeplink_entrypoint(user_id, chat_id)
            return
        if not self._event_visible_to_users(event):
            await self._send_invalid_deeplink_entrypoint(user_id, chat_id)
            return
        if not self.registration_service.has_profile_consent(user_id):
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    f"{DISCLAIMER_TEXT}\n\n"
                    f"После согласия открою мероприятие: «{event.title}»."
                ),
                attachments=inline_keyboard(
                    [
                        [
                            callback_button(
                                "✅ Согласен и открыть мероприятие",
                                Payload("consent_accept_event", value=slug),
                            )
                        ]
                    ]
                ),
            )
            return
        await self._send_event_detail(user_id, chat_id, event.id)

    async def _send_invalid_deeplink_entrypoint(
        self,
        user_id: int,
        chat_id: int | None,
    ) -> None:
        prefix = "Ссылка на мероприятие устарела или неверна.\n\n"
        if not self.registration_service.has_profile_consent(user_id):
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=f"{prefix}{DISCLAIMER_TEXT}",
                attachments=consent_keyboard(),
            )
            return
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=f"{prefix}Откройте каталог и выберите мероприятие заново.",
            attachments=inline_keyboard([[callback_button("📚 Каталог", Payload("catalog"))]]),
        )

    async def _send_entrypoint(self, user_id: int, chat_id: int | None) -> None:
        if not self.registration_service.has_profile_consent(user_id):
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=DISCLAIMER_TEXT,
                attachments=consent_keyboard(),
            )
            return
        await self._send_main_menu(user_id, chat_id)

    async def _send_main_menu(self, user_id: int, chat_id: int | None) -> None:
        rows = [
            [callback_button("📚 Мероприятия", Payload("catalog"))],
            [callback_button("🎫 Мои записи", Payload("my_regs"))],
        ]
        if self.organizer_service.can_use_menu(user_id):
            rows.append([callback_button("🧑‍💼 Меню организатора", Payload("org_menu"))])
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=self._main_menu_text(user_id),
            attachments=[
                image_attachment(BotImageAsset.MAIN_MENU),
                *inline_keyboard(rows),
            ],
        )

    def _main_menu_text(self, user_id: int) -> str:
        return f"{MAIN_MENU_HEADER_TEXT}\n{self._available_commands_text(user_id)}"

    def _unknown_command_text(self, user_id: int) -> str:
        return f"Я понимаю команды:\n{self._available_commands_text(user_id)}"

    def _available_commands_text(self, user_id: int) -> str:
        lines = list(BASE_COMMAND_LINES)
        if self.organizer_service.can_use_menu(user_id):
            lines.extend(ORGANIZER_COMMAND_LINES)
        return "\n".join(lines)

    async def _send_catalog(
        self,
        user_id: int,
        chat_id: int | None,
        *,
        page: int = 0,
    ) -> None:
        if not self.registration_service.has_profile_consent(user_id):
            raise ConsentRequiredError("Нужно согласие")
        events = [
            event
            for event in self.registration_service.list_events()
            if (
                not event.registration_closed
                and self.registration_service.available_places_for_event(event) > 0
            )
        ]
        if not events:
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    "📚 Книга мероприятий\n"
                    "Страница 1/1\n\n"
                    "Пока в книге нет ближайших мероприятий. Загляните позже: "
                    "как только появятся новые мероприятия, они будут здесь."
                ),
                attachments=[
                    image_attachment(BotImageAsset.MAIN_MENU),
                    *inline_keyboard([[self._main_menu_button()]]),
                ],
            )
            return
        total_pages = max(ceil(len(events) / CATALOG_PAGE_SIZE), 1)
        page = max(min(page, total_pages - 1), 0)
        page_events = events[
            page * CATALOG_PAGE_SIZE : (page + 1) * CATALOG_PAGE_SIZE
        ]
        lines = ["📚 Книга мероприятий", f"Страница {page + 1}/{total_pages}"]
        rows: list[list[dict]] = []
        current_detail_row: list[dict] = []

        def add_event_to_catalog(offset: int, event: Event) -> None:
            dev_line = f"\n[DEV] event_id={event.id}" if self.dev_mode else ""
            lines.append(
                f"\n{offset}. {event.title}{dev_line}\n"
                f"📅 {self._format_datetime(event.starts_at)}\n"
                f"🕒 {event.duration_minutes} мин. · {self._format_event_format(event)}"
            )
            current_detail_row.append(
                callback_button(
                    f"{offset}. {self._short_button_title(event.title)}",
                    Payload("event_detail", event_id=event.id, value=str(page)),
                )
            )
            if len(current_detail_row) == 2:
                rows.append(current_detail_row.copy())
                current_detail_row.clear()

        first_offset = 1 + page * CATALOG_PAGE_SIZE
        if page == 0:
            lines.extend(
                [
                    "",
                    "Здесь собраны все ближайшие мероприятия по датам: от самых скорых к дальним. "
                    "Листайте книгу кнопками ниже и открывайте подробности любого мероприятия.",
                ]
            )
            soon_events = page_events[:CATALOG_SOON_COUNT]
            other_events = page_events[CATALOG_SOON_COUNT:]
            if soon_events:
                lines.append("\n🔥 УЖЕ СКОРО")
                for offset, event in enumerate(soon_events, start=first_offset):
                    add_event_to_catalog(offset, event)
            if other_events:
                lines.append("\n📖 ДАЛЬШЕ В КНИГЕ")
                for offset, event in enumerate(
                    other_events,
                    start=first_offset + len(soon_events),
                ):
                    add_event_to_catalog(offset, event)
        else:
            for offset, event in enumerate(page_events, start=first_offset):
                add_event_to_catalog(offset, event)

        if current_detail_row:
            rows.append(current_detail_row)

        previous_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        rows.append(
            [
                callback_button("⬅️ Назад", Payload("catalog", value=str(previous_page))),
                callback_button("➡️ Далее", Payload("catalog", value=str(next_page))),
            ]
        )
        rows.append([self._main_menu_button()])
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text="\n".join(lines),
            attachments=[
                image_attachment(BotImageAsset.MAIN_MENU),
                *inline_keyboard(rows),
            ],
        )

    async def _send_event_detail(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
        *,
        page: int = 0,
    ) -> None:
        event = self.storage.get_event(event_id)
        if event is None:
            raise RegistrationClosedError("Мероприятие недоступно")
        if not self._event_visible_to_users(event):
            await self._send_event_unavailable_for_user(user_id, chat_id)
            return
        free = self.registration_service.available_places_for_event(event)
        dev_line = f"\n[DEV] event_id={event.id}" if self.dev_mode else ""
        active_registration = self._active_registration_for_event(user_id, event.id)
        rows: list[list[dict]] = []
        status_lines: list[str] = []
        if active_registration is not None:
            status_lines.extend(
                [
                    "✅ ВЫ УЖЕ ЗАПИСАНЫ НА ЭТО МЕРОПРИЯТИЕ.",
                    f"Код записи: {active_registration.code}",
                ]
            )
            if event.registration_closed:
                status_lines.append(REGISTRATION_CLOSED_ACTIVE_RECORD_TEXT)
            rows.append(
                [
                    callback_button(
                        "🎫 Мои записи",
                        Payload("my_regs"),
                        intent="positive",
                    )
                ]
            )
            rows.append(
                [
                    callback_button(
                        "❌ Отменить запись",
                        Payload(
                            "reg_cancel_confirm",
                            event_id=event.id,
                            registration_id=active_registration.id,
                        ),
                        intent="negative",
                    )
                ]
            )
        elif event.registration_closed:
            status_lines.append("Регистрация закрыта.")
        elif event.starts_at <= self.now():
            status_lines.append("Мероприятие уже началось.")
        elif free <= 0:
            status_lines.append("Свободных мест нет.")
        else:
            rows.append(
                [
                    callback_button(
                        "📝 Записаться",
                        Payload("event_book", event_id=event.id),
                        intent="positive",
                    )
                ]
            )

        deeplink = await self._event_deeplink(event)
        share_text = "\n\n🔗 Ссылка: Нажмите чтобы скопировать" if deeplink else ""
        if deeplink:
            rows.append([clipboard_button("🔗 Поделиться", deeplink)])
        rows.append(
            [callback_button("⬅️ К каталогу", Payload("catalog", value=str(page)))]
        )
        status_text = "\n".join(status_lines)
        status_text = f"\n\n{status_text}" if status_text else ""
        detail_attachments = self._event_detail_attachments(event, rows)
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=self._event_public_text(
                event,
                free_places=free,
                dev_line=dev_line,
                status_text=status_text,
                share_text=share_text,
            ),
            attachments=detail_attachments,
        )

    async def _send_booking_step(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
    ) -> None:
        event = self.storage.get_event(event_id, with_slots=True, with_image=False)
        if event is None:
            raise RegistrationClosedError("Мероприятие недоступно")
        if event.registration_closed or not self._event_visible_to_users(event):
            raise RegistrationClosedError("Регистрация на мероприятие закрыта")
        if self.registration_service.available_places_for_event(event) <= 0:
            raise NoSeatsAvailableError("Свободных мест нет")
        if event.slots:
            rows = []
            for slot in event.slots:
                free = self.registration_service.available_places_for_event(event, slot.id)
                if free > 0:
                    rows.append(
                        [
                            callback_button(
                                f"🕙 {slot.title} · {free} мест",
                                Payload("slot_pick", event_id=event.id, slot_id=slot.id),
                            )
                        ]
                    )
            rows.append([callback_button("⬅️ К каталогу", Payload("catalog"))])
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=f"Выберите слот для мероприятия «{event.title}».",
                attachments=inline_keyboard(rows),
            )
            return
        await self._send_registration_summary(user_id, chat_id, event_id, None)

    async def _send_registration_summary(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
        slot_id: int | None,
    ) -> None:
        event = self.storage.get_event(event_id, with_slots=True, with_image=False)
        if event is None:
            raise RegistrationClosedError("Мероприятие недоступно")
        if event.registration_closed or not self._event_visible_to_users(event):
            raise RegistrationClosedError("Регистрация на мероприятие закрыта")
        slot_text = "без отдельного слота"
        if slot_id is not None:
            slot = next((item for item in event.slots if item.id == slot_id), None)
            slot_text = slot.title if slot else "выбранный слот"
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=(
                "🧾 Проверьте запись перед подтверждением:\n\n"
                f"Мероприятие: {event.title}\n"
                f"📅 Дата: {self._format_datetime(event.starts_at)}\n"
                f"Слот: {slot_text}\n"
                f"Формат: {self._format_event_format(event)}\n\n"
                "Запись можно отменить до начала мероприятия."
            ),
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "✅ Подтвердить",
                            Payload("register_confirm", event_id=event.id, slot_id=slot_id),
                        )
                    ],
                    [callback_button("⬅️ К каталогу", Payload("catalog"))],
                ]
            ),
        )

    async def _send_registration_success(
        self,
        user_id: int,
        chat_id: int | None,
        registration: Registration,
    ) -> None:
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=(
                "✅ Запись подтверждена.\n"
                f"Мероприятие: {registration.event.title}\n"
                f"Ваш код записи: {registration.code}\n\n"
                "Покажите этот код организатору на входе."
            ),
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "ℹ️ Мероприятие",
                            Payload("event_detail", event_id=registration.event_id),
                        )
                    ],
                    [
                        callback_button(
                            "🔕 Уведомления",
                            Payload(
                                "notif_toggle",
                                registration_id=registration.id,
                                value="off",
                            ),
                        )
                    ],
                    [callback_button("🎫 Мои записи", Payload("my_regs"))],
                ]
            ),
        )

    async def _send_my_registrations(
        self,
        user_id: int,
        chat_id: int | None,
    ) -> None:
        registrations = self._visible_user_registrations(user_id)
        if not registrations:
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text="🎫 У вас пока нет записей.",
                attachments=inline_keyboard(
                    [
                        [callback_button("📚 Каталог", Payload("catalog"))],
                        [self._main_menu_button()],
                    ]
                ),
            )
            return
        lines = ["🎫 Ваши записи:"]
        rows = []
        for index, registration in enumerate(registrations, start=1):
            closed_registration_text = (
                f"\n{REGISTRATION_CLOSED_ACTIVE_RECORD_TEXT}"
                if (
                    registration.status in ACTIVE_REGISTRATION_STATUSES
                    and registration.event.registration_closed
                )
                else ""
            )
            lines.append(
                f"\n{registration.event.title}\n"
                f"Код: {registration.code}\n"
                f"Статус: {self._format_status_for_user(registration.status)}"
                f"{closed_registration_text}"
            )
            is_active = registration.status in ACTIVE_REGISTRATION_STATUSES
            prefix = "✅" if is_active else "⚪"
            intent = "positive" if is_active else "default"
            rows.append(
                [
                    callback_button(
                        f"{prefix} {index} ℹ️ {self._short_button_title(registration.event.title)}",
                        Payload("event_detail", event_id=registration.event_id),
                        intent=intent,
                    )
                ]
            )
        rows.append([callback_button("📚 Каталог", Payload("catalog"))])
        rows.append([self._main_menu_button()])
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text="\n".join(lines),
            attachments=inline_keyboard(rows),
        )

    def _visible_user_registrations(self, user_id: int) -> list[Registration]:
        registrations = [
            registration
            for registration in self.registration_service.list_user_registrations(user_id)
            if registration.event is not None and self._event_visible_to_users(registration.event)
        ]
        active_event_ids = {
            registration.event_id
            for registration in registrations
            if registration.status in ACTIVE_REGISTRATION_STATUSES
        }
        return [
            registration
            for registration in registrations
            if (
                registration.status in ACTIVE_REGISTRATION_STATUSES
                or registration.event_id not in active_event_ids
            )
        ]

    async def _send_cancel_confirmation(
        self,
        user_id: int,
        chat_id: int | None,
        registration_id: int,
    ) -> None:
        registration = self.storage.get_registration(registration_id)
        if registration is None or registration.user_id != user_id:
            raise RegistrationNotFoundError("Запись не найдена")
        if registration.event is not None and not self._event_visible_to_users(registration.event):
            await self._send_event_unavailable_for_user(user_id, chat_id)
            return
        event_title = registration.event.title if registration.event else "мероприятие"
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=(
                "⚠️ ОТМЕНА ЗАПИСИ\n\n"
                "Вы отменяете запись на мероприятие:\n"
                f"{event_title}\n"
                f"Код записи: {registration.code}\n\n"
                "После подтверждения запись будет отменена, а место станет доступно другим."
            ),
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "❌ Да, отменить запись",
                            Payload(
                                "reg_cancel_apply",
                                event_id=registration.event_id,
                                registration_id=registration.id,
                            ),
                            intent="negative",
                        )
                    ],
                    [
                        callback_button(
                            "✅ Оставить запись",
                            Payload("event_detail", event_id=registration.event_id),
                            intent="positive",
                        )
                    ],
                ]
            ),
        )

    async def _send_organizer_menu(
        self,
        user_id: int,
        chat_id: int | None,
        *,
        page: int = 0,
    ) -> None:
        events = self.organizer_service.list_events(user_id)
        if not events and not self.organizer_service.can_use_menu(user_id):
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text="У вас нет доступа к меню организатора.",
                attachments=inline_keyboard([[self._main_menu_button()]]),
            )
            return
        upcoming_events = [event for event in events if self._event_visible_to_users(event)]
        past_events = [event for event in events if not self._event_visible_to_users(event)]
        book_events = [
            *[("upcoming", event) for event in upcoming_events],
            *[("past", event) for event in past_events],
        ]
        rows: list[list[dict]] = []

        if not book_events:
            text = (
                "📚 Книга мероприятий Организатора\n"
                "Страница 1/1\n\n"
                "✨ Пока в книге Организатора нет мероприятий. "
                "Создайте первое — оно появится здесь сразу после заполнения."
            )
            rows.append(
                [callback_button(CREATE_EVENT_BUTTON_TEXT, Payload("org_create"))]
            )
            rows.append([self._main_menu_button()])
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=text,
                attachments=[
                    image_attachment(BotImageAsset.ORGANIZER_MENU),
                    *inline_keyboard(rows),
                ],
            )
            return

        total_pages = max(ceil(len(book_events) / ORGANIZER_BOOK_PAGE_SIZE), 1)
        page = max(min(page, total_pages - 1), 0)
        first_offset = 1 + page * ORGANIZER_BOOK_PAGE_SIZE
        page_events = book_events[
            page * ORGANIZER_BOOK_PAGE_SIZE : (page + 1) * ORGANIZER_BOOK_PAGE_SIZE
        ]
        lines = [
            "📚 Книга мероприятий Организатора",
            f"Страница {page + 1}/{total_pages}",
            "",
            "Листайте книгу кнопками ниже и открывайте нужное мероприятие. 🗂️",
        ]
        current_section: str | None = None
        current_button_row: list[dict] = []
        section_titles = {
            "upcoming": "🔥 БЛИЖАЙШИЕ",
            "past": "🕘 ПРОШЕДШИЕ",
        }

        for offset, (section, event) in enumerate(page_events, start=first_offset):
            if section != current_section:
                lines.append(f"\n{section_titles[section]}")
                current_section = section
            dev_line = f"\n[DEV] event_id={event.id}" if self.dev_mode else ""
            lines.append(
                f"\n{offset}. {event.title}{dev_line}\n"
                f"📅 {self._format_datetime(event.starts_at)}\n"
                f"🕒 {event.duration_minutes} мин. · {self._format_event_format(event)}"
            )
            if section == "past":
                lines.append(
                    f"🧹 Удалится через {self._days_until_event_cleanup(event)} дн."
                )
            button_title = self._short_button_title(
                event.title,
                max_chars=ORGANIZER_BOOK_BUTTON_TITLE_MAX_CHARS,
            )
            current_button_row.append(
                callback_button(
                    f"⚙️ {offset}. {button_title}",
                    Payload("org_event", event_id=event.id, value=str(page)),
                )
            )
            if len(current_button_row) == 2:
                rows.append(current_button_row)
                current_button_row = []

        if current_button_row:
            rows.append(current_button_row)

        previous_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        rows.append(
            [
                callback_button(
                    "⬅️ Назад",
                    Payload("org_menu", value=str(previous_page)),
                ),
                callback_button(
                    "➡️ Далее",
                    Payload("org_menu", value=str(next_page)),
                ),
            ]
        )
        rows.append(
            [callback_button(CREATE_EVENT_BUTTON_TEXT, Payload("org_create"))]
        )
        rows.append([self._main_menu_button()])
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text="\n".join(lines),
            attachments=[
                image_attachment(BotImageAsset.ORGANIZER_MENU),
                *inline_keyboard(rows),
            ],
        )

    async def _send_organizer_event(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
        *,
        page: int = 0,
    ) -> None:
        registrations = self.organizer_service.get_event_registrations(user_id, event_id)
        event = self.storage.get_event(event_id)
        assert event is not None
        deeplink = await self._event_deeplink(event) if self._event_visible_to_users(event) else None
        reminder_buttons = [
            callback_button(
                "🔔 Напомнить участникам",
                Payload("org_remind", event_id=event_id),
            )
        ]
        if deeplink:
            reminder_buttons.append(clipboard_button("🔗 Поделиться", deeplink))
        rows = [
            [
                callback_button(
                    "🗓 Изменить дату или время",
                    Payload("org_datetime", event_id=event_id),
                ),
                callback_button(
                    "📍 Изменить место",
                    Payload("org_place", event_id=event_id),
                )
            ],
            reminder_buttons,
        ]
        if registrations:
            rows.append(
                [
                    callback_button(
                        "👥 Участники",
                        Payload("org_participants", event_id=event_id, value=str(page)),
                    ),
                    callback_button(
                        "🔎 Отметить по коду",
                        Payload(
                            "org_attendance_lookup",
                            event_id=event_id,
                            value=str(page),
                        ),
                    ),
                ]
            )
        close_buttons = []
        if self._event_visible_to_users(event) and not event.registration_closed:
            close_buttons.append(
                callback_button(
                    "🚫 Закрыть регистрацию",
                    Payload("org_close_confirm", event_id=event_id),
                    intent="negative",
                )
            )
        if self._event_visible_to_users(event):
            close_buttons.append(
                callback_button(
                    "🛑 Закрыть мероприятие",
                    Payload("org_event_close_confirm", event_id=event_id),
                    intent="negative",
                )
            )
        if close_buttons:
            rows.append(close_buttons)
        rows.append(
            [
                callback_button(
                    "📝 Заполнить информацию заново",
                    Payload("org_rebuild", event_id=event_id),
                )
            ]
        )
        rows.append([callback_button("⬅️ Назад", Payload("org_menu", value=str(page)))])
        free = self.registration_service.available_places_for_event(event)
        share_text = "\n\n🔗 Ссылка: Нажмите чтобы скопировать" if deeplink else ""
        status_text = ""
        if not self._event_visible_to_users(event):
            status_text = (
                "\n\nМероприятие уже началось или завершилось. "
                f"В меню организатора оно будет ещё {self._days_until_event_cleanup(event)} дн."
            )
        elif event.registration_closed:
            status_text = "\n\nРегистрация новых участников закрыта."
        public_text = self._event_public_text(
            event,
            free_places=free,
            status_text=status_text,
            share_text=share_text,
        )
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=f"🧑‍💼 МЕНЮ ОРГАНИЗАТОРА\n\n{public_text}",
            attachments=self._event_detail_attachments(event, rows),
        )

    async def _send_organizer_participants(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
        *,
        page: int = 0,
    ) -> None:
        event = self._organizer_event_for_actor(
            user_id,
            event_id,
            with_slots=False,
            with_image=False,
        )
        registrations = self._sorted_participant_registrations(
            self.organizer_service.get_event_registrations(user_id, event_id)
        )
        total_pages = max(ceil(len(registrations) / PARTICIPANTS_BOOK_PAGE_SIZE), 1)
        page = max(min(page, total_pages - 1), 0)
        first_offset = 1 + page * PARTICIPANTS_BOOK_PAGE_SIZE
        page_registrations = registrations[
            page * PARTICIPANTS_BOOK_PAGE_SIZE : (page + 1) * PARTICIPANTS_BOOK_PAGE_SIZE
        ]
        lines = [
            "👥 Участники мероприятия",
            f"Страница {page + 1}/{total_pages}",
            "",
            f"Мероприятие: {self._markdown_text(event.title)}",
            (
                "Здесь видны записи на мероприятие: профиль участника, код и текущий "
                "статус."
            ),
            "Нажмите на профиль записанного участника, чтобы отметить, что он пришел.",
        ]
        if page_registrations:
            lines.append("")
            for offset, registration in enumerate(page_registrations, start=first_offset):
                lines.append(
                    f"{offset}. {self._participant_profile_link(registration)} - "
                    f"{registration.code} - "
                    f"{self._format_participant_status(registration.status)}"
                )
        else:
            lines.append("\nПока по этому мероприятию нет записей.")

        rows: list[list[dict]] = []
        current_row: list[dict] = []
        for registration in page_registrations:
            if registration.status not in {
                RegistrationStatus.CONFIRMED,
                RegistrationStatus.ATTENDED,
            }:
                continue
            button_name = self._short_button_title(
                self._participant_name(registration),
                max_chars=PARTICIPANT_BUTTON_NAME_MAX_CHARS,
            )
            if registration.status == RegistrationStatus.CONFIRMED:
                current_row.append(
                    callback_button(
                        f"✅ {button_name} пришел",
                        Payload(
                            "org_participant_attended",
                            event_id=event.id,
                            registration_id=registration.id,
                            value=str(page),
                        ),
                        intent="positive",
                    )
                )
            else:
                current_row.append(
                    callback_button(
                        f"↩️ {button_name} записан",
                        Payload(
                            "org_participant_confirmed",
                            event_id=event.id,
                            registration_id=registration.id,
                            value=str(page),
                        ),
                        intent="default",
                    )
                )
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)

        previous_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        rows.append(
            [
                callback_button(
                    "⬅️ Назад",
                    Payload(
                        "org_participants",
                        event_id=event.id,
                        value=str(previous_page),
                    ),
                ),
                callback_button(
                    "➡️ Далее",
                    Payload(
                        "org_participants",
                        event_id=event.id,
                        value=str(next_page),
                    ),
                ),
            ]
        )
        rows.append(
            [
                callback_button(
                    "⬅️ К мероприятию",
                    Payload("org_event", event_id=event.id),
                )
            ]
        )
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text="\n".join(lines),
            attachments=[
                image_attachment(BotImageAsset.PARTICIPANTS_MENU),
                *inline_keyboard(rows),
            ],
            format="markdown",
        )

    async def _start_attendance_lookup(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
        *,
        page: int = 0,
    ) -> None:
        self._organizer_event_for_actor(
            user_id,
            event_id,
            with_slots=False,
            with_image=False,
        )
        state = OrganizerState(
            user_id=user_id,
            mode=STATE_ATTENDANCE_LOOKUP,
            event_id=event_id,
            step=STATE_ATTENDANCE_LOOKUP,
            data={"page": page},
            updated_at=self.now(),
        )
        self.storage.set_organizer_state(state)
        await self._send_attendance_lookup_prompt(user_id, chat_id, state)

    async def _handle_attendance_lookup_message(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        text: str,
    ) -> None:
        if not text:
            await self._send_attendance_lookup_prompt(
                user_id,
                chat_id,
                state,
                prefix="Отправьте код или профильную ссылку одним сообщением.",
            )
            return
        assert state.event_id is not None
        participant_user_id = extract_max_user_id(text)
        try:
            if participant_user_id is not None:
                registration = self.organizer_service.mark_attended_by_event_user(
                    user_id,
                    state.event_id,
                    participant_user_id,
                )
            else:
                if self._looks_like_max_profile_link(text):
                    await self._send_attendance_lookup_prompt(
                        user_id,
                        chat_id,
                        state,
                        prefix=(
                            "Не нашел user_id в ссылке. Отправьте профильную ссылку "
                            "из списка участников или код вида 123-456."
                        ),
                    )
                    return
                code = normalize_registration_code_input(text)
                if code is None:
                    await self._send_attendance_lookup_prompt(
                        user_id,
                        chat_id,
                        state,
                        prefix=(
                            "Не разобрал код. Подойдет формат 123-456 или 123456."
                        ),
                    )
                    return
                registration = self.organizer_service.mark_attended_by_event_code(
                    user_id,
                    state.event_id,
                    code,
                )
        except BotDomainError as exc:
            await self._send_attendance_lookup_prompt(
                user_id,
                chat_id,
                state,
                prefix=self._friendly_error(exc),
            )
            return

        await self._send_attendance_lookup_prompt(
            user_id,
            chat_id,
            state,
            prefix=(
                f"Запись {self._participant_name(registration)} отмечена как пришедшая.\n"
                "Можно отправить следующий код или профиль."
            ),
        )

    async def _send_attendance_lookup_prompt(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        *,
        prefix: str | None = None,
    ) -> None:
        assert state.event_id is not None
        event = self._organizer_event_for_actor(
            user_id,
            state.event_id,
            with_slots=False,
            with_image=False,
        )
        text = (
            "🔎 Отметка по коду\n\n"
            f"Мероприятие: {event.title}\n"
            "Отправьте код вида 123-456 или профильную ссылку MAX вида "
            "max://user/123.\n"
            "После успешной отметки можно отправлять следующий код или профиль."
        )
        if prefix:
            text = f"{prefix}\n\n{text}"
        page = int(state.data.get("page") or 0)
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "⬅️ К мероприятию",
                            Payload("org_event", event_id=event.id, value=str(page)),
                        )
                    ]
                ]
            ),
        )

    async def _send_organizer_close_confirmation(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
    ) -> None:
        event = self._organizer_event_for_actor(
            user_id,
            event_id,
            with_slots=False,
            with_image=False,
        )
        if event.registration_closed:
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=f"Регистрация на «{event.title}» уже закрыта.",
                attachments=inline_keyboard(
                    [
                        [
                            callback_button(
                                "ℹ️ Мероприятие",
                                Payload("org_event", event_id=event.id),
                            )
                        ]
                    ]
                ),
            )
            return
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=(
                f"Закрыть регистрацию на мероприятие «{event.title}»?\n\n"
                "Новые участники больше не увидят мероприятие в каталоге "
                "и не смогут записаться. Текущие записи останутся действующими: "
                "участники останутся на мероприятии и получат уведомления."
            ),
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "🚫 Закрыть регистрацию",
                            Payload("org_close", event_id=event.id),
                            intent="negative",
                        )
                    ],
                    [callback_button("⬅️ Назад", Payload("org_event", event_id=event.id))],
                ]
            ),
        )

    async def _send_organizer_event_close_confirmation(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
    ) -> None:
        event = self._organizer_event_for_actor(
            user_id,
            event_id,
            with_slots=False,
            with_image=False,
        )
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=(
                f"Закрыть мероприятие «{event.title}»?\n\n"
                "Мероприятие будет закрыто, все активные записи будут отменены, "
                "и все участники получат уведомление."
            ),
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "🛑 Закрыть мероприятие",
                            Payload("org_event_close", event_id=event.id),
                            intent="negative",
                        )
                    ],
                    [callback_button("⬅️ Назад", Payload("org_event", event_id=event.id))],
                ]
            ),
        )

    async def _send_organizer_reminder_entry(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
    ) -> None:
        event = self._organizer_event_for_actor(
            user_id,
            event_id,
            with_slots=True,
            with_image=False,
        )
        if not event.slots:
            await self._start_manual_reminder_text(
                user_id,
                chat_id,
                event.id,
                slot_id=None,
            )
            return
        rows = [
            [
                callback_button(
                    "🔔 Всем записавшимся",
                    Payload("org_remind_all", event_id=event.id),
                )
            ]
        ]
        for slot in event.slots:
            rows.append(
                [
                    callback_button(
                        f"🕙 Слот {slot.title}",
                        Payload("org_remind_slot", event_id=event.id, slot_id=slot.id),
                    )
                ]
            )
        rows.append(
            [callback_button("⬅️ Назад", Payload("org_event", event_id=event.id))]
        )
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=f"Кому отправить напоминание по мероприятию «{event.title}»?",
            attachments=inline_keyboard(rows),
        )

    async def _start_manual_reminder_text(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
        *,
        slot_id: int | None,
    ) -> None:
        event = self._organizer_event_for_actor(
            user_id,
            event_id,
            with_slots=True,
            with_image=False,
        )
        if slot_id is not None and not any(slot.id == slot_id for slot in event.slots):
            raise SlotNotFoundError("Слот не найден")
        self.storage.set_organizer_state(
            OrganizerState(
                user_id=user_id,
                mode=STATE_MANUAL_REMINDER_TEXT,
                event_id=event_id,
                step=STATE_MANUAL_REMINDER_TEXT,
                data={"slot_id": slot_id},
                updated_at=self.now(),
            )
        )
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=(
                "Отправьте текст напоминания одним сообщением.\n\n"
                "Если свой текст не нужен, нажмите «Использовать автотекст»."
            ),
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "🔔 Использовать автотекст",
                            Payload("org_remind_auto"),
                        )
                    ],
                    [
                        callback_button(
                            "⬅️ Назад",
                            Payload("org_event", event_id=event_id),
                        )
                    ],
                ]
            ),
        )

    async def _finish_manual_reminder_text(
        self,
        user_id: int,
        chat_id: int | None,
        *,
        custom_text: str | None,
    ) -> None:
        state = self.storage.get_organizer_state(user_id)
        if (
            state is None
            or state.mode != STATE_MANUAL_REMINDER_TEXT
            or state.event_id is None
        ):
            await self._send_organizer_menu(user_id, chat_id)
            return
        created = self.organizer_service.enqueue_manual_reminder(
            user_id,
            state.event_id,
            slot_id=state.data.get("slot_id"),
            custom_text=custom_text,
        )
        event_id = state.event_id
        self.storage.clear_organizer_state(user_id)
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=f"Напоминание поставлено в очередь для {len(created)} участников.",
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "🧑‍💼 Открыть мероприятие",
                            Payload("org_event", event_id=event_id),
                        )
                    ]
                ]
            ),
        )

    async def _handle_pending_event_image(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
        attachments: list | None,
    ) -> None:
        image = self._first_image_attachment(attachments)
        if image is None:
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text="Жду картинку. Отправьте изображение одним сообщением или откройте меню заново.",
            )
            return
        token, url = image
        event = self.storage.set_event_image(
            user_id,
            event_id,
            token=token,
            url=url,
            now=self.now(),
        )
        self.storage.clear_pending_event_image(user_id)
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=f"Картинка обновлена для мероприятия «{event.title}».",
            attachments=inline_keyboard(
                [[callback_button("🧑‍💼 Открыть мероприятие", Payload("org_event", event_id=event.id))]]
            ),
        )

    async def _send_organizer_datetime_menu(
        self,
        user_id: int,
        chat_id: int | None,
        event_id: int,
    ) -> None:
        event = self._organizer_event_for_actor(
            user_id,
            event_id,
            with_slots=False,
            with_image=False,
        )
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=f"Что изменить у мероприятия «{event.title}»?",
            attachments=inline_keyboard(
                [
                    [callback_button("🗓 Изменить дату", Payload("org_edit_date", event_id=event_id))],
                    [callback_button("⏰ Изменить время", Payload("org_edit_time", event_id=event_id))],
                    [callback_button("⬅️ Назад", Payload("org_event", event_id=event_id))],
                ]
            ),
        )

    async def _start_simple_organizer_state(
        self,
        user_id: int,
        chat_id: int | None,
        mode: str,
        event_id: int,
        prompt: str,
    ) -> None:
        self._organizer_event_for_actor(
            user_id,
            event_id,
            with_slots=False,
            with_image=False,
        )
        self.storage.set_organizer_state(
            OrganizerState(
                user_id=user_id,
                mode=mode,
                event_id=event_id,
                step=mode,
                data={},
                updated_at=self.now(),
            )
        )
        await self._send(user_id=user_id, chat_id=chat_id, text=prompt)

    async def _start_event_builder(
        self,
        user_id: int,
        chat_id: int | None,
        *,
        mode: str,
        event_id: int | None = None,
    ) -> None:
        if mode == BUILDER_MODE_EDIT:
            assert event_id is not None
            self._organizer_event_for_actor(
                user_id,
                event_id,
                with_slots=False,
                with_image=False,
            )
        elif not self.organizer_service.can_use_menu(user_id):
            raise AccessDeniedError("Нет доступа к созданию мероприятий")
        state = OrganizerState(
            user_id=user_id,
            mode=mode,
            event_id=event_id,
            step="title",
            data={},
            updated_at=self.now(),
        )
        self.storage.set_organizer_state(state)
        await self._send_builder_prompt(user_id, chat_id, state)

    async def _handle_organizer_state_message(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        text: str,
        attachments: list | None,
    ) -> None:
        if state.mode == STATE_EDIT_PLACE:
            await self._handle_edit_place_message(user_id, chat_id, state, text)
            return
        if state.mode in {STATE_EDIT_DATE, STATE_EDIT_TIME}:
            await self._handle_edit_datetime_message(user_id, chat_id, state, text)
            return
        if state.mode == STATE_MANUAL_REMINDER_TEXT:
            await self._finish_manual_reminder_text(
                user_id,
                chat_id,
                custom_text=text,
            )
            return
        if state.mode == STATE_ATTENDANCE_LOOKUP:
            await self._handle_attendance_lookup_message(user_id, chat_id, state, text)
            return
        if state.mode in {BUILDER_MODE_CREATE, BUILDER_MODE_EDIT}:
            await self._handle_builder_message(user_id, chat_id, state, text, attachments)
            return
        self.storage.clear_organizer_state(user_id)
        await self._send_organizer_menu(user_id, chat_id)

    async def _handle_edit_place_message(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        text: str,
    ) -> None:
        if not text:
            await self._send(user_id=user_id, chat_id=chat_id, text="Отправьте место или ссылку одним сообщением.")
            return
        assert state.event_id is not None
        event = self.organizer_service.update_event_location(user_id, state.event_id, text)
        self.storage.clear_organizer_state(user_id)
        await self._send_organizer_event(user_id, chat_id, event.id)

    async def _handle_edit_datetime_message(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        text: str,
    ) -> None:
        assert state.event_id is not None
        event = self._organizer_event_for_actor(
            user_id,
            state.event_id,
            with_slots=False,
            with_image=False,
        )
        local_start = event.starts_at.astimezone(MOSCOW_TZ)
        try:
            if state.mode == STATE_EDIT_DATE:
                day = parse_organizer_date(
                    text,
                    today=self.now().astimezone(MOSCOW_TZ).date(),
                )
                clock = time(local_start.hour, local_start.minute)
            else:
                day = local_start.date()
                clock = parse_organizer_time(text)
            starts_at = combine_moscow_datetime(day, clock)
        except ValueError:
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text="Не разобрал значение. Пример даты: 12.03 или 12 марта. Пример времени: 12:30.",
            )
            return
        if not self._event_start_is_future(starts_at):
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    "Дата и время уже прошли. "
                    "Укажите момент позже текущего московского времени."
                ),
            )
            return
        event = self.organizer_service.reschedule_event(user_id, event.id, starts_at)
        self.storage.clear_organizer_state(user_id)
        await self._send_organizer_event(user_id, chat_id, event.id)

    async def _handle_builder_callback(
        self,
        user_id: int,
        chat_id: int | None,
        data: Payload,
    ) -> None:
        state = self.storage.get_organizer_state(user_id)
        if state is None or state.mode not in {BUILDER_MODE_CREATE, BUILDER_MODE_EDIT}:
            await self._send(user_id=user_id, chat_id=chat_id, text="Откройте меню организатора заново.")
            return
        if data.action == "org_builder_cancel":
            await self._send_builder_cancel_confirmation(user_id, chat_id, state)
            return
        if data.action == "org_builder_cancel_back":
            await self._send_builder_prompt(user_id, chat_id, state)
            return
        if data.action == "org_builder_cancel_confirm":
            await self._cancel_builder(user_id, chat_id, state)
            return
        if data.action == "org_builder_current":
            await self._take_current_builder_value(user_id, chat_id, state)
            return
        if data.action == "org_builder_format" and state.step == "format":
            try:
                value = EventFormat(data.value or "")
            except ValueError:
                await self._send_builder_prompt(user_id, chat_id, state, prefix="Выберите формат кнопкой.")
                return
            await self._advance_builder(user_id, chat_id, state, "format", value.value, "location")
            return
        if data.action == "org_builder_no_slots" and state.step == "slots_intro":
            await self._advance_builder(user_id, chat_id, state, "slots", [], "image")
            return
        if data.action == "org_builder_slots_add" and state.step in {"slots_intro", "slot_next"}:
            state.step = "slot_start"
            self._save_builder_state(state)
            await self._send_builder_prompt(user_id, chat_id, state)
            return
        if data.action == "org_builder_slots_done" and state.step in {"slots_intro", "slot_next"}:
            state.data.setdefault("slots", [])
            state.step = "image"
            self._save_builder_state(state)
            await self._send_builder_prompt(user_id, chat_id, state)
            return
        if data.action == "org_builder_skip_image" and state.step == "image":
            state.data["image_token"] = None
            state.data["image_url"] = None
            await self._finish_builder(user_id, chat_id, state)
            return
        await self._send_builder_prompt(user_id, chat_id, state)

    async def _handle_builder_message(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        text: str,
        attachments: list | None,
    ) -> None:
        if state.step in {"title", "description", "requirements", "location"}:
            if not text:
                await self._send_builder_prompt(user_id, chat_id, state, prefix="Отправьте текст одним сообщением.")
                return
            next_step = {
                "title": "description",
                "description": "requirements",
                "requirements": "date",
                "location": "slots_intro",
            }[state.step]
            key = "location_or_url" if state.step == "location" else state.step
            await self._advance_builder(user_id, chat_id, state, key, text, next_step)
            return
        if state.step == "date":
            try:
                local_today = self.now().astimezone(MOSCOW_TZ).date()
                day = parse_organizer_date(text, today=local_today)
            except ValueError:
                await self._send_builder_prompt(user_id, chat_id, state, prefix="Не разобрал дату.")
                return
            if day < local_today:
                await self._send_builder_prompt(
                    user_id,
                    chat_id,
                    state,
                    prefix="Дата уже прошла. Введите сегодняшнюю или будущую дату.",
                )
                return
            value = day.isoformat()
            await self._advance_builder(user_id, chat_id, state, "date", value, "time")
            return
        if state.step == "time":
            try:
                clock = parse_organizer_time(text)
            except ValueError:
                await self._send_builder_prompt(user_id, chat_id, state, prefix="Не разобрал время.")
                return
            day = date.fromisoformat(str(state.data["date"]))
            if not self._event_start_is_future(combine_moscow_datetime(day, clock)):
                await self._send_builder_prompt(
                    user_id,
                    chat_id,
                    state,
                    prefix="Время уже прошло. Введите время позже текущего московского.",
                )
                return
            value = self._serialize_time(clock)
            await self._advance_builder(user_id, chat_id, state, "time", value, "duration")
            return
        if state.step == "duration":
            value = self._positive_int(text)
            if value is None:
                await self._send_builder_prompt(user_id, chat_id, state, prefix="Отправьте длительность числом минут.")
                return
            await self._advance_builder(user_id, chat_id, state, "duration_minutes", value, "capacity")
            return
        if state.step == "capacity":
            value = self._positive_int(text)
            if value is None:
                await self._send_builder_prompt(user_id, chat_id, state, prefix="Отправьте лимит мест числом.")
                return
            await self._advance_builder(user_id, chat_id, state, "capacity_total", value, "format")
            return
        if state.step == "slots_intro" and text:
            state.step = "slot_start"
            await self._handle_slot_time(user_id, chat_id, state, text, "pending_slot_start", "slot_end")
            return
        if state.step in {"slots_intro", "slot_next"}:
            await self._send_builder_prompt(user_id, chat_id, state, prefix="Выберите действие кнопкой.")
            return
        if state.step == "slot_start":
            await self._handle_slot_time(user_id, chat_id, state, text, "pending_slot_start", "slot_end")
            return
        if state.step == "slot_end":
            await self._handle_slot_time(user_id, chat_id, state, text, "pending_slot_end", "slot_capacity")
            return
        if state.step == "slot_capacity":
            await self._handle_slot_capacity(user_id, chat_id, state, text)
            return
        if state.step == "format":
            await self._send_builder_prompt(user_id, chat_id, state, prefix="Выберите формат кнопкой.")
            return
        if state.step == "image":
            image = self._first_image_attachment(attachments)
            if image is None:
                await self._send_builder_prompt(
                    user_id,
                    chat_id,
                    state,
                    prefix="Жду картинку. Отправьте изображение одним сообщением или нажмите «Пропустить».",
                )
                return
            state.data["image_token"], state.data["image_url"] = image
            await self._finish_builder(user_id, chat_id, state)

    async def _handle_slot_time(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        text: str,
        key: str,
        next_step: str,
    ) -> None:
        try:
            value = self._serialize_time(parse_organizer_time(text))
        except ValueError:
            await self._send_builder_prompt(user_id, chat_id, state, prefix="Не разобрал время слота.")
            return
        if key == "pending_slot_end":
            start = time.fromisoformat(str(state.data.get("pending_slot_start")))
            end = time.fromisoformat(value)
            if end <= start:
                await self._send_builder_prompt(user_id, chat_id, state, prefix="Конец слота должен быть позже начала.")
                return
        state.data[key] = value
        state.step = next_step
        self._save_builder_state(state)
        await self._send_builder_prompt(user_id, chat_id, state)

    async def _handle_slot_capacity(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        text: str,
    ) -> None:
        capacity = self._positive_int(text)
        if capacity is None:
            await self._send_builder_prompt(user_id, chat_id, state, prefix="Отправьте лимит слота числом.")
            return
        day = date.fromisoformat(str(state.data["date"]))
        start = time.fromisoformat(str(state.data["pending_slot_start"]))
        end = time.fromisoformat(str(state.data["pending_slot_end"]))
        slots = list(state.data.get("slots") or [])
        slots.append(
            {
                "id": 0,
                "title": self._serialize_time(start),
                "starts_at": combine_moscow_datetime(day, start).isoformat(),
                "ends_at": combine_moscow_datetime(day, end).isoformat(),
                "capacity": capacity,
            }
        )
        state.data["slots"] = slots
        state.data.pop("pending_slot_start", None)
        state.data.pop("pending_slot_end", None)
        state.step = "slot_next"
        self._save_builder_state(state)
        await self._send_builder_prompt(user_id, chat_id, state, prefix="Слот добавлен.")

    async def _advance_builder(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        key: str,
        value,
        next_step: str,
    ) -> None:
        state.data[key] = value
        state.step = next_step
        self._save_builder_state(state)
        await self._send_builder_prompt(user_id, chat_id, state)

    async def _take_current_builder_value(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
    ) -> None:
        if state.mode != BUILDER_MODE_EDIT or state.event_id is None:
            await self._send_builder_prompt(user_id, chat_id, state)
            return
        event = self._organizer_event_for_actor(
            user_id,
            state.event_id,
            with_slots=state.step == "slots_intro",
            with_image=state.step == "image",
        )
        local_start = event.starts_at.astimezone(MOSCOW_TZ)
        current_values = {
            "title": ("title", event.title, "description"),
            "description": ("description", event.description, "requirements"),
            "requirements": ("requirements", event.requirements, "date"),
            "date": ("date", local_start.date().isoformat(), "time"),
            "time": ("time", f"{local_start.hour:02d}:{local_start.minute:02d}", "duration"),
            "duration": ("duration_minutes", event.duration_minutes, "capacity"),
            "capacity": ("capacity_total", event.capacity_total, "format"),
            "format": ("format", event.format.value, "location"),
            "location": ("location_or_url", event.location_or_url, "slots_intro"),
        }
        if state.step == "slots_intro":
            state.data["slots"] = [self._serialize_slot(slot) for slot in event.slots]
            state.step = "image"
            self._save_builder_state(state)
            await self._send_builder_prompt(user_id, chat_id, state)
            return
        if state.step == "image":
            state.data["image_token"] = event.image_token
            state.data["image_url"] = event.image_url
            await self._finish_builder(user_id, chat_id, state)
            return
        current = current_values.get(state.step)
        if current is None:
            await self._send_builder_prompt(user_id, chat_id, state)
            return
        key, value, next_step = current
        await self._advance_builder(user_id, chat_id, state, key, value, next_step)

    async def _finish_builder(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
    ) -> None:
        event = self._event_from_builder_state(state)
        slots = self._slots_from_builder_state(state)
        if not self._event_start_is_future(event.starts_at):
            state.step = "time"
            self._save_builder_state(state)
            await self._send_builder_prompt(
                user_id,
                chat_id,
                state,
                prefix="Дата и время уже прошли. Укажите будущий момент.",
            )
            return
        if state.mode == BUILDER_MODE_CREATE:
            saved = self.organizer_service.create_event(
                user_id,
                event,
                slots=slots,
                image_token=state.data.get("image_token"),
                image_url=state.data.get("image_url"),
            )
        else:
            saved = self.organizer_service.replace_event(
                user_id,
                event,
                slots=slots,
                image_token=state.data.get("image_token"),
                image_url=state.data.get("image_url"),
            )
        self.storage.clear_organizer_state(user_id)
        await self._send_organizer_event(user_id, chat_id, saved.id)

    async def _send_builder_cancel_confirmation(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
    ) -> None:
        action_text = "создание" if state.mode == BUILDER_MODE_CREATE else "изменение"
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=f"Точно отменить заполнение? Сейчас идёт {action_text} мероприятия.",
            attachments=inline_keyboard(
                [
                    [callback_button("✅ ДА, ОТМЕНИТЬ", Payload("org_builder_cancel_confirm"), intent="negative")],
                    [callback_button("↩️ НЕТ, ВЕРНУТЬСЯ НАЗАД", Payload("org_builder_cancel_back"), intent="positive")],
                ]
            ),
        )

    async def _cancel_builder(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
    ) -> None:
        event_id = state.event_id
        mode = state.mode
        self.storage.clear_organizer_state(user_id)
        if mode == BUILDER_MODE_EDIT and event_id is not None:
            await self._send_organizer_event(user_id, chat_id, event_id)
            return
        await self._send_organizer_menu(user_id, chat_id)

    async def _send_builder_prompt(
        self,
        user_id: int,
        chat_id: int | None,
        state: OrganizerState,
        *,
        prefix: str | None = None,
    ) -> None:
        prompt = self._builder_prompt_text(state)
        if prefix:
            prompt = f"{prefix}\n\n{prompt}"
        rows = self._builder_prompt_rows(user_id, state)
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=prompt,
            attachments=inline_keyboard(rows) if rows else None,
        )

    def _builder_prompt_text(self, state: OrganizerState) -> str:
        prompts = {
            "title": "Введите название мероприятия.",
            "description": "Введите описание мероприятия.",
            "requirements": "Введите требования для участников.",
            "date": "Введите дату мероприятия.",
            "time": "Введите время начала.",
            "duration": "Введите длительность в минутах.",
            "capacity": "Введите общий лимит мест.",
            "format": "Выберите формат мероприятия.",
            "location": "Введите место или ссылку.",
            "slots_intro": (
                "🧩 Слоты — это отдельные временные окна внутри одного мероприятия. "
                "Например, две группы экскурсий: 10:00-11:00 и 11:15-12:15.\n\n"
                "Если мероприятие одно общее, нажмите «🚫 Без слотов». "
                "Если нужны группы по времени, нажмите «➕ Добавить слот» "
                "и затем введите начало, конец и лимит мест."
            ),
            "slot_start": "Введите время начала слота.",
            "slot_end": "Введите время конца слота.",
            "slot_capacity": "Введите лимит мест для этого слота.",
            "slot_next": "Слот добавлен. Можно добавить ещё слот или завершить слоты.",
            "image": "Отправьте картинку одним сообщением или нажмите «Пропустить».",
        }
        return prompts[state.step]

    def _builder_prompt_rows(self, user_id: int, state: OrganizerState) -> list[list[dict]]:
        rows: list[list[dict]] = []
        if self._builder_current_available(state):
            rows.append([callback_button(TAKE_CURRENT_TEXT, Payload("org_builder_current"))])
        if state.step == "format":
            rows.extend(
                [
                    [callback_button("🏫 Очно", Payload("org_builder_format", value=EventFormat.IN_PERSON.value))],
                    [callback_button("💻 Онлайн", Payload("org_builder_format", value=EventFormat.ONLINE.value))],
                ]
            )
        elif state.step == "slots_intro":
            if not self._event_has_active_registrations(user_id, state):
                rows.append([callback_button("🚫 Без слотов", Payload("org_builder_no_slots"))])
                rows.append([callback_button("➕ Добавить слот", Payload("org_builder_slots_add"))])
        elif state.step == "slot_next":
            rows.extend(
                [
                    [callback_button("➕ Добавить слот", Payload("org_builder_slots_add"))],
                    [callback_button("✅ Завершить слоты", Payload("org_builder_slots_done"))],
                ]
            )
        elif state.step == "image":
            rows.append([callback_button("➡️ Пропустить", Payload("org_builder_skip_image"))])
        rows.append([callback_button(self._builder_cancel_button_text(state), Payload("org_builder_cancel"), intent="negative")])
        return rows

    def _builder_current_available(self, state: OrganizerState) -> bool:
        return (
            state.mode == BUILDER_MODE_EDIT
            and state.event_id is not None
            and state.step
            in {
                "title",
                "description",
                "requirements",
                "date",
                "time",
                "duration",
                "capacity",
                "format",
                "location",
                "slots_intro",
                "image",
            }
        )

    def _event_has_active_registrations(self, user_id: int, state: OrganizerState) -> bool:
        if state.mode != BUILDER_MODE_EDIT or state.event_id is None:
            return False
        return any(
            registration.status in ACTIVE_REGISTRATION_STATUSES
            for registration in self.organizer_service.get_event_registrations(user_id, state.event_id)
        )

    def _save_builder_state(self, state: OrganizerState) -> None:
        state.updated_at = self.now()
        self.storage.set_organizer_state(state)

    def _event_from_builder_state(self, state: OrganizerState) -> Event:
        day = date.fromisoformat(str(state.data["date"]))
        clock = time.fromisoformat(str(state.data["time"]))
        current = (
            self.storage.get_event(
                state.event_id,
                with_slots=False,
                with_image=False,
            )
            if state.event_id is not None
            else None
        )
        cancellation_policy_text = current.cancellation_policy_text if current is not None else ""
        late_cancel_policy = current.late_cancel_policy if current is not None else LateCancelPolicy.DENY
        return Event(
            id=state.event_id or 0,
            title=str(state.data["title"]),
            description=str(state.data["description"]),
            requirements=str(state.data["requirements"]),
            starts_at=combine_moscow_datetime(day, clock),
            duration_minutes=int(state.data["duration_minutes"]),
            format=EventFormat(str(state.data["format"])),
            location_or_url=str(state.data["location_or_url"]),
            cancellation_policy_text=cancellation_policy_text,
            capacity_total=int(state.data["capacity_total"]),
            late_cancel_policy=late_cancel_policy,
            created_at=self.now(),
        )

    def _event_visible_to_users(self, event: Event) -> bool:
        return self._event_start_is_future(event.starts_at)

    def _event_start_is_future(self, starts_at: datetime) -> bool:
        return starts_at > self.now()

    async def _send_event_unavailable_for_user(
        self,
        user_id: int,
        chat_id: int | None,
    ) -> None:
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=(
                "Мероприятие уже началось или завершилось. "
                "Откройте каталог и выберите ближайшее мероприятие."
            ),
            attachments=inline_keyboard(
                [
                    [callback_button("📚 Каталог", Payload("catalog"))],
                    [self._main_menu_button()],
                ]
            ),
        )

    def _days_until_event_cleanup(self, event: Event) -> int:
        expires_at = event.starts_at + timedelta(days=ORGANIZER_EVENT_RETENTION_DAYS)
        remaining_seconds = (expires_at - self.now()).total_seconds()
        return max(ceil(remaining_seconds / 86400), 0)

    def _event_public_text(
        self,
        event: Event,
        *,
        free_places: int,
        dev_line: str = "",
        status_text: str = "",
        share_text: str = "",
    ) -> str:
        return (
            f"ℹ️ {event.title}{dev_line}\n\n"
            f"{event.description}\n\n"
            f"📅 {self._format_datetime(event.starts_at)}\n"
            f"🕒 {event.duration_minutes} мин. · {self._format_event_format(event)}\n"
            f"📌 Требования: {event.requirements}\n"
            f"📍 Адрес/ссылка: {event.location_or_url}\n"
            f"✅ Свободных мест: {self._format_places_count(event, free_places)}"
            f"{status_text}"
            f"{share_text}"
        )

    @staticmethod
    def _format_places_count(event: Event, free_places: int) -> str:
        maximum_places = (
            sum(slot.capacity for slot in event.slots)
            if event.slots
            else event.capacity_total
        )
        return f"{free_places} из {maximum_places}"

    @staticmethod
    def _builder_cancel_button_text(state: OrganizerState) -> str:
        if state.mode == BUILDER_MODE_EDIT:
            return "❌ Отменить изменение"
        return "❌ Отменить создание"

    def _slots_from_builder_state(self, state: OrganizerState) -> list[EventSlot]:
        slots = []
        for item in state.data.get("slots") or []:
            slots.append(
                EventSlot(
                    id=int(item.get("id") or 0),
                    event_id=state.event_id or 0,
                    title=str(item.get("title") or ""),
                    starts_at=datetime.fromisoformat(str(item["starts_at"])),
                    ends_at=datetime.fromisoformat(str(item["ends_at"])),
                    capacity=int(item.get("capacity") or 0),
                    booked_count=int(item.get("booked_count") or 0),
                )
            )
        return slots

    @staticmethod
    def _serialize_slot(slot: EventSlot) -> dict:
        return {
            "id": slot.id,
            "title": slot.title,
            "starts_at": slot.starts_at.isoformat(),
            "ends_at": slot.ends_at.isoformat(),
            "capacity": slot.capacity,
            "booked_count": slot.booked_count,
        }

    @staticmethod
    def _serialize_time(value: time) -> str:
        return f"{value.hour:02d}:{value.minute:02d}"

    @staticmethod
    def _positive_int(raw: str) -> int | None:
        try:
            value = int((raw or "").strip())
        except ValueError:
            return None
        return value if value > 0 else None

    def _organizer_event_for_actor(
        self,
        user_id: int,
        event_id: int,
        *,
        with_slots: bool = True,
        with_image: bool = True,
    ) -> Event:
        self.organizer_service.get_event_registrations(user_id, event_id)
        event = self.storage.get_event(
            event_id,
            with_slots=with_slots,
            with_image=with_image,
        )
        if event is None:
            raise RegistrationClosedError("Мероприятие недоступно")
        return event

    async def _send_find_result(
        self,
        user_id: int,
        chat_id: int | None,
        code: str,
    ) -> None:
        try:
            registration = self.organizer_service.find_registration_by_code_any_event(
                user_id,
                code,
            )
        except (AccessDeniedError, BotDomainError) as exc:
            await self._send(
                user_id=user_id,
                chat_id=chat_id,
                text=self._friendly_error(exc),
            )
            return
        await self._send(
            user_id=user_id,
            chat_id=chat_id,
            text=(
                f"Запись найдена: {registration.code}\n"
                f"Мероприятие: {registration.event.title}\n"
                f"Участник: {registration.user.display_name}\n"
                f"Статус: {self._format_status(registration.status)}"
            ),
            attachments=inline_keyboard(
                [
                    [
                        callback_button(
                            "✅ Пришёл",
                            Payload("org_attended", registration_id=registration.id),
                        )
                    ]
                ]
            ),
        )

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        if isinstance(exc, ConsentRequiredError):
            return "Сначала нужно согласиться на обработку минимальных данных профиля."
        if isinstance(exc, DuplicateActiveRegistrationError):
            return "Вы уже записаны на это мероприятие."
        if isinstance(exc, NoSeatsAvailableError):
            return "Свободных мест уже нет."
        if isinstance(exc, RegistrationClosedError):
            return "Регистрация закрыта."
        if isinstance(exc, SlotNotFoundError):
            return "Слот не найден."
        if isinstance(exc, LateCancellationDeniedError):
            return "После начала мероприятия отмена недоступна."
        if isinstance(exc, AccessDeniedError):
            return "У вас нет доступа к этому действию."
        if isinstance(exc, EventStartInPastError):
            return "Дата и время мероприятия уже прошли."
        if isinstance(exc, AttendanceMarkDeniedError):
            return "Отмененную запись нельзя отметить как пришедшую."
        return str(exc)

    @staticmethod
    def _format_event_format(event: Event) -> str:
        return "онлайн" if event.format.value == "online" else "очно"

    @staticmethod
    def _format_status(status: RegistrationStatus) -> str:
        mapping = {
            RegistrationStatus.CONFIRMED: "Подтверждена",
            RegistrationStatus.CANCELED_BY_USER: "Отменена пользователем",
            RegistrationStatus.CANCELED_BY_ORGANIZER: "Отменена организатором",
            RegistrationStatus.LATE_CANCELED: "Поздняя отмена",
            RegistrationStatus.ATTENDED: "Пришёл",
        }
        return mapping[status]

    @staticmethod
    def _format_participant_status(status: RegistrationStatus) -> str:
        if status == RegistrationStatus.CONFIRMED:
            return "Записан"
        if status == RegistrationStatus.ATTENDED:
            return "Пришел"
        return "Запись отменена"

    @classmethod
    def _sorted_participant_registrations(
        cls,
        registrations: list[Registration],
    ) -> list[Registration]:
        status_order = {
            RegistrationStatus.CONFIRMED: 0,
            RegistrationStatus.ATTENDED: 1,
        }
        return sorted(
            registrations,
            key=lambda item: (
                status_order.get(item.status, 2),
                cls._participant_name(item).casefold(),
                item.code,
            ),
        )

    @staticmethod
    def _participant_name(registration: Registration) -> str:
        if registration.user is not None and registration.user.display_name.strip():
            return registration.user.display_name.strip()
        return f"Участник {registration.user_id}"

    @classmethod
    def _participant_profile_link(cls, registration: Registration) -> str:
        name = cls._markdown_link_text(cls._participant_name(registration))
        return f"[{name}](max://user/{registration.user_id})"

    @staticmethod
    def _markdown_text(value: str) -> str:
        return "".join(f"\\{char}" if char in "\\[]()" else char for char in value)

    @staticmethod
    def _markdown_link_text(value: str) -> str:
        return "".join(f"\\{char}" if char in "\\[]()" else char for char in value)

    @staticmethod
    def _looks_like_max_profile_link(value: str) -> bool:
        normalized = (value or "").casefold()
        return "max://user/" in normalized or "max.ru/" in normalized

    @classmethod
    def _format_status_for_user(cls, status: RegistrationStatus) -> str:
        icon = "✅" if status in ACTIVE_REGISTRATION_STATUSES else "⚪"
        return f"{icon} {cls._format_status(status)}"

    @staticmethod
    def _short_button_title(title: str, *, max_chars: int = 22) -> str:
        normalized = " ".join(title.split())
        if len(normalized) <= max_chars:
            return normalized
        clipped = normalized[:max_chars].rstrip()
        if " " in clipped:
            clipped = clipped.rsplit(" ", 1)[0]
        return f"{clipped}..."

    @staticmethod
    def _main_menu_button() -> dict:
        return callback_button("🏠 Главное меню", Payload("main_menu"))

    @classmethod
    def _event_detail_attachments(cls, event: Event, rows: list[list[dict]]) -> list[dict]:
        attachments: list[dict] = []
        image = cls._event_image_attachment(event)
        if image is not None:
            attachments.append(image)
        attachments.extend(inline_keyboard(rows))
        return attachments

    @staticmethod
    def _event_image_attachment(event: Event) -> dict | None:
        if event.image_token:
            return {"type": "image", "payload": {"token": event.image_token}}
        if event.image_url:
            return {"type": "image", "payload": {"url": event.image_url}}
        return None

    @staticmethod
    def _first_image_attachment(attachments: list | None) -> tuple[str | None, str | None] | None:
        for attachment in attachments or []:
            if not isinstance(attachment, dict) or attachment.get("type") != "image":
                continue
            payload = attachment.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            token = payload.get("token")
            url = payload.get("url")
            photos = payload.get("photos")
            if not token and isinstance(photos, dict):
                first_photo = next(iter(photos.values()), None)
                if isinstance(first_photo, dict):
                    token = first_photo.get("token")
            token = str(token).strip() if token else None
            url = str(url).strip() if url else None
            if token or url:
                return token, url
        return None

    async def _send(
        self,
        *,
        user_id: int,
        chat_id: int | None,
        text: str,
        attachments: list | None = None,
        format: str | None = None,
    ) -> None:
        lock = _send_lock_for(user_id)
        with measure("send_lock_wait"):
            await lock.acquire()
        try:
            await self._send_unlocked(
                user_id=user_id,
                chat_id=chat_id,
                text=text,
                attachments=attachments,
                format=format,
            )
        finally:
            lock.release()

    async def _send_unlocked(
        self,
        *,
        user_id: int,
        chat_id: int | None,
        text: str,
        attachments: list | None = None,
        format: str | None = None,
    ) -> None:
        if self._source_message_id is not None:
            try:
                edit_kwargs = {
                    "message_id": self._source_message_id,
                    "text": text,
                    "attachments": attachments,
                    "notify": False,
                }
                if format is not None:
                    edit_kwargs["format"] = format
                message_id = await self.bot.edit_message(**edit_kwargs)
                self.storage.set_last_bot_message_id(
                    user_id,
                    message_id or self._source_message_id,
                    now=self.now(),
                )
                await self._delete_source_user_message()
                return
            except Exception:
                pass
        previous_message_id = self.storage.get_last_bot_message_id(user_id)
        if previous_message_id:
            try:
                await self.bot.delete_message(message_id=previous_message_id)
            except Exception:
                pass
        send_kwargs = {
            "user_id": user_id,
            "chat_id": chat_id,
            "text": text,
            "attachments": attachments,
        }
        if format is not None:
            send_kwargs["format"] = format
        message_id = await self.bot.send_message(**send_kwargs)
        if message_id:
            self.storage.set_last_bot_message_id(user_id, message_id, now=self.now())
        await self._delete_source_user_message()

    async def _delete_source_user_message(self) -> None:
        message_id = self._source_user_message_id
        self._source_user_message_id = None
        if not message_id:
            return
        try:
            await self.bot.delete_message(message_id=message_id)
        except Exception:
            pass

    @staticmethod
    def _payload_page(data: Payload) -> int:
        try:
            return int(data.value or "0")
        except ValueError:
            return 0

    def _active_registration_for_event(
        self,
        user_id: int,
        event_id: int,
    ) -> Registration | None:
        for registration in self.registration_service.list_user_registrations(user_id):
            if (
                registration.event_id == event_id
                and registration.status in ACTIVE_REGISTRATION_STATUSES
            ):
                return registration
        return None

    async def _event_deeplink(self, event: Event) -> str | None:
        username = await self._max_bot_username()
        if not username:
            return None
        slug = self.storage.get_event_slug(event.id)
        if slug is None:
            slug = self._assign_default_event_slug(event)
        if slug is None:
            return None
        try:
            return build_event_deeplink(username, slug)
        except ValueError:
            return None

    async def _max_bot_username(self) -> str:
        configured = (self.max_bot_username or "").strip().removeprefix("@")
        if configured:
            return configured
        getter = getattr(self.bot, "get_bot_username", None)
        if getter is None:
            return ""
        try:
            username = await getter()
        except Exception:
            return ""
        return (username or "").strip().removeprefix("@")

    def _assign_default_event_slug(self, event: Event) -> str | None:
        existing = self.storage.get_event_slug(event.id)
        if existing is not None:
            return existing
        base_slug = build_default_event_slug(event.title, event.starts_at)
        candidates = [base_slug, self._slug_with_suffix(base_slug, str(event.id))]
        for slug in dict.fromkeys(candidates):
            try:
                self.storage.assign_event_slug(event.id, slug, now=self.now())
                return slug
            except DuplicateEventSlugError:
                existing = self.storage.get_event_slug(event.id)
                if existing is not None:
                    return existing
        return self.storage.get_event_slug(event.id)

    @staticmethod
    def _slug_with_suffix(slug: str, suffix: str) -> str:
        max_slug_length = MAX_START_PAYLOAD_LIMIT - len(EVENT_PAYLOAD_PREFIX)
        suffix = suffix.strip("-") or "event"
        max_base_length = max_slug_length - len(suffix) - 1
        if max_base_length <= 0:
            return slug[:max_slug_length].strip("-")
        return f"{slug[:max_base_length].strip('-')}-{suffix}"

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        return value.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
