from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.domain import (
    EventStartInPastError,
    InvalidNotificationKindError,
    SlotNotFoundError,
)
from app.enums import (
    ACTIVE_REGISTRATION_STATUSES,
    MANUAL_NOTIFICATION_KINDS,
    NotificationKind,
    RegistrationStatus,
)
from app.storage.base import Storage
from app.storage.entities import Event, EventSlot, NotificationOutbox, Registration


class OrganizerService:
    def __init__(
        self,
        storage: Storage,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.now = now or (lambda: datetime.now(timezone.utc))

    def list_events(self, actor_user_id: int) -> list[Event]:
        return self.storage.list_organizer_events(actor_user_id)

    def can_use_menu(self, actor_user_id: int) -> bool:
        return self.storage.has_role(actor_user_id, "organizer") or self.storage.has_role(actor_user_id, "admin")

    def get_event_registrations(
        self,
        actor_user_id: int,
        event_id: int,
    ) -> list[Registration]:
        return self.storage.get_event_registrations(actor_user_id, event_id)

    def find_registration_by_code(
        self,
        actor_user_id: int,
        event_id: int,
        code: str,
    ) -> Registration:
        return self.storage.find_registration_by_code(actor_user_id, event_id, code)

    def find_registration_by_code_any_event(
        self,
        actor_user_id: int,
        code: str,
    ) -> Registration:
        return self.storage.find_registration_by_code_any_event(actor_user_id, code)

    def close_registration(self, actor_user_id: int, event_id: int) -> Event:
        return self.storage.close_registration(actor_user_id, event_id)

    def reschedule_event(
        self,
        actor_user_id: int,
        event_id: int,
        starts_at: datetime,
    ) -> Event:
        self._ensure_future_start(starts_at)
        previous = self.storage.get_event(event_id)
        previous_start = previous.starts_at if previous is not None else None
        event = self.storage.reschedule_event(
            actor_user_id,
            event_id,
            starts_at,
            now=self.now(),
        )
        if previous_start is not None and previous_start != starts_at:
            self.enqueue_manual_notification(
                actor_user_id,
                event_id,
                NotificationKind.TIME_CHANGED,
            )
        return event

    def update_event_location(
        self,
        actor_user_id: int,
        event_id: int,
        location_or_url: str,
    ) -> Event:
        previous = self.storage.get_event(event_id)
        previous_location = previous.location_or_url if previous is not None else None
        event = self.storage.update_event_location(
            actor_user_id,
            event_id,
            location_or_url,
            now=self.now(),
        )
        if previous_location is not None and previous_location != event.location_or_url:
            self.enqueue_manual_notification(
                actor_user_id,
                event_id,
                NotificationKind.VENUE_CHANGED,
            )
        return event

    def create_event(
        self,
        actor_user_id: int,
        event: Event,
        *,
        slots: list[EventSlot],
        image_token: str | None,
        image_url: str | None,
    ) -> Event:
        self._ensure_future_start(event.starts_at)
        return self.storage.create_organizer_event(
            actor_user_id,
            event,
            slots=slots,
            image_token=image_token,
            image_url=image_url,
            now=self.now(),
        )

    def replace_event(
        self,
        actor_user_id: int,
        event: Event,
        *,
        slots: list[EventSlot],
        image_token: str | None,
        image_url: str | None,
    ) -> Event:
        self._ensure_future_start(event.starts_at)
        previous = self.storage.get_event(event.id)
        previous_start = previous.starts_at if previous is not None else None
        previous_location = previous.location_or_url if previous is not None else None
        updated = self.storage.replace_organizer_event(
            actor_user_id,
            event,
            slots=slots,
            image_token=image_token,
            image_url=image_url,
            now=self.now(),
        )
        if previous_start is not None and previous_start != updated.starts_at:
            self.enqueue_manual_notification(
                actor_user_id,
                updated.id,
                NotificationKind.TIME_CHANGED,
            )
        if previous_location is not None and previous_location != updated.location_or_url:
            self.enqueue_manual_notification(
                actor_user_id,
                updated.id,
                NotificationKind.VENUE_CHANGED,
            )
        return updated

    def mark_attended(self, actor_user_id: int, registration_id: int) -> Registration:
        return self.storage.mark_attended(
            actor_user_id,
            registration_id,
            now=self.now(),
        )

    def change_status(
        self,
        actor_user_id: int,
        registration_id: int,
        status: RegistrationStatus,
    ) -> Registration:
        return self.storage.change_status(
            actor_user_id,
            registration_id,
            status,
            now=self.now(),
        )

    def enqueue_manual_notification(
        self,
        actor_user_id: int,
        event_id: int,
        kind: NotificationKind,
    ) -> list[NotificationOutbox]:
        if kind not in MANUAL_NOTIFICATION_KINDS:
            raise InvalidNotificationKindError("Этот тип уведомления нельзя отправить вручную")
        event = self.storage.get_event(event_id)
        message_text = self._render_manual_notification(kind, event)
        return self.storage.enqueue_manual_notification(
            actor_user_id=actor_user_id,
            event_id=event_id,
            kind=kind,
            message_text=message_text,
            now=self.now(),
        )

    def enqueue_manual_reminder(
        self,
        actor_user_id: int,
        event_id: int,
        *,
        slot_id: int | None,
        custom_text: str | None,
        starts_in_text: str | None = None,
    ) -> list[NotificationOutbox]:
        event = self.storage.get_event(event_id)
        if event is not None and slot_id is not None and not any(
            slot.id == slot_id for slot in event.slots
        ):
            raise SlotNotFoundError("Слот не найден")
        registrations = self.storage.get_event_registrations(actor_user_id, event_id)
        created: list[NotificationOutbox] = []
        current = self.now()
        for registration in registrations:
            if registration.status not in ACTIVE_REGISTRATION_STATUSES:
                continue
            if not registration.notifications_enabled:
                continue
            if slot_id is not None and registration.slot_id != slot_id:
                continue
            created.append(
                self.storage.add_notification(
                    NotificationOutbox(
                        id=0,
                        event_id=event_id,
                        registration_id=registration.id,
                        user_id=registration.user_id,
                        kind=NotificationKind.MANUAL_REMINDER,
                        message_text=self._render_manual_reminder(
                            event or registration.event,
                            registration,
                            custom_text=custom_text,
                            starts_in_text=starts_in_text,
                        ),
                        send_after=current,
                        created_at=current,
                    )
                )
            )
        return created

    @staticmethod
    def _render_manual_notification(kind: NotificationKind, event: Event | None) -> str:
        title = event.title if event else "мероприятию"
        if kind == NotificationKind.TIME_CHANGED:
            return f"Обновление по мероприятию «{title}»: изменилось время. Проверьте карточку записи."
        if kind == NotificationKind.VENUE_CHANGED:
            return f"Обновление по мероприятию «{title}»: изменилась аудитория или место проведения."
        if kind == NotificationKind.JOIN_LINK_CHANGED:
            return f"Обновление по мероприятию «{title}»: обновлена ссылка на подключение."
        raise InvalidNotificationKindError("Неподдерживаемый тип уведомления")

    @staticmethod
    def _render_manual_reminder(
        event: Event | None,
        registration: Registration,
        *,
        custom_text: str | None,
        starts_in_text: str | None,
    ) -> str:
        clean_text = (custom_text or "").strip()
        if not clean_text:
            clean_starts_in = (starts_in_text or "").strip()
            if clean_starts_in:
                clean_text = f"Напоминание: мероприятие начнётся примерно через {clean_starts_in}."
            else:
                clean_text = "Напоминание: мероприятие скоро начнётся."
        title = event.title if event else "Мероприятие"
        lines = [clean_text, "", title]
        if registration.slot is not None:
            lines.append(f"Слот: {registration.slot.title}")
        lines.append(f"Код записи: {registration.code}")
        if event is not None:
            lines.append(f"Место/ссылка: {event.location_or_url}")
        return "\n".join(lines)

    def _ensure_future_start(self, starts_at: datetime) -> None:
        if starts_at <= self.now():
            raise EventStartInPastError("Дата и время мероприятия уже прошли")
