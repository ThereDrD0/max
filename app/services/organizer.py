from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
from app.services.reminders import render_automatic_reminder, render_manual_reminder
from app.storage.base import Storage
from app.storage.entities import Event, EventSlot, NotificationOutbox, Registration


@dataclass(frozen=True, slots=True)
class EventCloseResult:
    event: Event
    notification_count: int


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

    def close_event(self, actor_user_id: int, event_id: int) -> EventCloseResult:
        event = self.storage.close_registration(actor_user_id, event_id)
        current = self.now()
        registrations = self.storage.get_event_registrations(actor_user_id, event_id)
        notification_count = 0
        for registration in registrations:
            if registration.status not in ACTIVE_REGISTRATION_STATUSES:
                continue
            self.storage.add_notification(
                NotificationOutbox(
                    id=0,
                    event_id=event_id,
                    registration_id=None,
                    user_id=registration.user_id,
                    kind=NotificationKind.EVENT_CLOSED,
                    message_text=self._render_event_closed_notification(event),
                    send_after=current,
                    created_at=current,
                )
            )
            notification_count += 1
            self.storage.change_status(
                actor_user_id,
                registration.id,
                RegistrationStatus.CANCELED_BY_ORGANIZER,
                now=current,
            )
        return EventCloseResult(event=event, notification_count=notification_count)

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
            self.storage.sync_registration_reminders(
                now=self.now(),
                render_reminder=render_automatic_reminder,
            )
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
            self.storage.sync_registration_reminders(
                now=self.now(),
                render_reminder=render_automatic_reminder,
            )
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

    def mark_attended_with_notification(
        self,
        actor_user_id: int,
        registration_id: int,
    ) -> Registration:
        previous = self.storage.get_registration(registration_id)
        previous_status = previous.status if previous is not None else None
        current = self.now()
        registration = self.storage.mark_attended(
            actor_user_id,
            registration_id,
            now=current,
        )
        should_notify = (
            previous_status == RegistrationStatus.CONFIRMED
            and registration.notifications_enabled
        )
        if should_notify:
            self.storage.add_notification(
                NotificationOutbox(
                    id=0,
                    event_id=registration.event_id,
                    registration_id=registration.id,
                    user_id=registration.user_id,
                    kind=NotificationKind.ATTENDANCE_MARKED,
                    message_text=self._render_attendance_marked_notification(
                        registration.event or self.storage.get_event(registration.event_id)
                    ),
                    send_after=current,
                    created_at=current,
                )
            )
        return registration

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
                        message_text=render_manual_reminder(
                            event or registration.event,
                            registration,
                            now=current,
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
    def _render_event_closed_notification(event: Event | None) -> str:
        title = event.title if event else "мероприятие"
        return (
            f"Мероприятие «{title}» закрыто Организатором.\n\n"
            "Ваша запись отменена. Приходить на это мероприятие не нужно."
        )

    @staticmethod
    def _render_attendance_marked_notification(event: Event | None) -> str:
        title = event.title if event else "мероприятие"
        return (
            f"Организатор отметил, что вы пришли на мероприятие «{title}».\n\n"
            "Спасибо, что отметились. Хорошего участия!"
        )

    def _ensure_future_start(self, starts_at: datetime) -> None:
        if starts_at <= self.now():
            raise EventStartInPastError("Дата и время мероприятия уже прошли")
