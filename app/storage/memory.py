from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock

from app.domain import (
    AccessDeniedError,
    BotDomainError,
    ConsentRequiredError,
    DuplicateActiveRegistrationError,
    DuplicateEventSlugError,
    EventNotFoundError,
    InvalidNotificationKindError,
    LateCancellationDeniedError,
    NoSeatsAvailableError,
    RegistrationClosedError,
    RegistrationNotFoundError,
    SlotNotFoundError,
    SlotRequiredError,
)
from app.enums import (
    ACTIVE_REGISTRATION_STATUSES,
    MANUAL_NOTIFICATION_KINDS,
    LateCancelPolicy,
    NotificationKind,
    OutboxStatus,
    RegistrationStatus,
)
from app.storage.base import CodeGenerator, ReminderRenderer
from app.storage.entities import (
    AuditLog,
    Consent,
    Event,
    EventSlot,
    NotificationOutbox,
    OrganizerEvent,
    OrganizerState,
    Registration,
    RoleAssignment,
    User,
    utc_now,
)


class MemoryStorage:
    def __init__(self) -> None:
        self._lock = RLock()
        self._ids = {
            "audit": 0,
            "consent": 0,
            "event": 0,
            "notification": 0,
            "organizer_event": 0,
            "registration": 0,
            "role": 0,
            "slot": 0,
        }
        self.users: dict[int, User] = {}
        self.consents: dict[int, Consent] = {}
        self.events: dict[int, Event] = {}
        self.slots: dict[int, EventSlot] = {}
        self.roles: dict[int, RoleAssignment] = {}
        self.organizer_events: dict[int, OrganizerEvent] = {}
        self.registrations: dict[int, Registration] = {}
        self.notifications: dict[int, NotificationOutbox] = {}
        self.audit_log: dict[int, AuditLog] = {}
        self.registration_codes: dict[str, int] = {}
        self.active_registration_keys: dict[str, int] = {}
        self.bot_sessions: dict[int, str] = {}
        self.event_deeplinks: dict[str, int] = {}
        self.event_images: dict[int, tuple[str | None, str | None, int, datetime]] = {}
        self.pending_event_images: dict[int, tuple[int, datetime]] = {}
        self.organizer_states: dict[int, OrganizerState] = {}

    def ready(self) -> bool:
        return True

    def _next_id(self, name: str, preferred: int = 0) -> int:
        if preferred:
            self._ids[name] = max(self._ids[name], preferred)
            return preferred
        self._ids[name] += 1
        return self._ids[name]

    def upsert_user(
        self,
        user_id: int,
        display_name: str,
        *,
        is_bot: bool = False,
        now: datetime | None = None,
    ) -> User:
        with self._lock:
            current = now or utc_now()
            user = self.users.get(user_id)
            if user is None:
                user = User(
                    user_id=user_id,
                    display_name=display_name or f"Пользователь {user_id}",
                    is_bot=is_bot,
                    created_at=current,
                    updated_at=current,
                )
                self.users[user_id] = user
            else:
                user.display_name = display_name or user.display_name
                user.is_bot = is_bot
                user.updated_at = current
            return user

    def get_user(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    def get_last_bot_message_id(self, user_id: int) -> str | None:
        return self.bot_sessions.get(user_id)

    def set_last_bot_message_id(
        self,
        user_id: int,
        message_id: str | None,
        *,
        now: datetime | None = None,
    ) -> None:
        with self._lock:
            if message_id is None:
                self.bot_sessions.pop(user_id, None)
                return
            self.bot_sessions[user_id] = message_id

    def record_profile_consent(
        self,
        user_id: int,
        document_version: str,
        *,
        now: datetime,
    ) -> Consent:
        with self._lock:
            self._ensure_user_exists(user_id, now=now)
            consent = Consent(
                id=self._next_id("consent"),
                user_id=user_id,
                document_version=document_version,
                profile_data_allowed=True,
                created_at=now,
            )
            self.consents[consent.id] = consent
            self._audit(user_id, "consent.accepted", "user", str(user_id), now=now)
            return consent

    def has_profile_consent(self, user_id: int) -> bool:
        return any(
            item.user_id == user_id and item.profile_data_allowed
            for item in self.consents.values()
        )

    def add_event(self, event: Event, *, slots: list[EventSlot] | None = None) -> Event:
        with self._lock:
            event.id = self._next_id("event", event.id)
            event.slots = []
            self.events[event.id] = event
            for slot in slots or []:
                slot.id = self._next_id("slot", slot.id)
                slot.event_id = event.id
                self.slots[slot.id] = slot
                event.slots.append(slot)
            return event

    def get_event(self, event_id: int) -> Event | None:
        event = self.events.get(event_id)
        if event is not None:
            event.slots = self._event_slots(event_id)
            self._attach_event_image(event)
        return event

    def assign_event_slug(
        self,
        event_id: int,
        slug: str,
        *,
        now: datetime | None = None,
    ) -> None:
        with self._lock:
            self._require_event(event_id)
            existing_event_id = self.event_deeplinks.get(slug)
            if existing_event_id is not None:
                if existing_event_id == event_id:
                    return
                raise DuplicateEventSlugError("Slug мероприятия уже используется")
            existing_slug = self.get_event_slug(event_id)
            if existing_slug is not None and existing_slug != slug:
                raise DuplicateEventSlugError("У мероприятия уже есть другой slug")
            self.event_deeplinks[slug] = event_id

    def get_event_by_slug(self, slug: str) -> Event | None:
        event_id = self.event_deeplinks.get(slug)
        if event_id is None:
            return None
        return self.get_event(event_id)

    def get_event_slug(self, event_id: int) -> str | None:
        for slug, linked_event_id in self.event_deeplinks.items():
            if linked_event_id == event_id:
                return slug
        return None

    def update_event_start(self, event_id: int, starts_at: datetime) -> None:
        with self._lock:
            event = self._require_event(event_id)
            event.starts_at = starts_at

    def reschedule_event(
        self,
        actor_user_id: int,
        event_id: int,
        starts_at: datetime,
        *,
        now: datetime,
    ) -> Event:
        with self._lock:
            event = self._require_event_access(actor_user_id, event_id)
            event.starts_at = starts_at
            self._audit(
                actor_user_id,
                "event.rescheduled",
                "event",
                str(event_id),
                {"starts_at": starts_at.isoformat()},
                now=now,
            )
            return event

    def update_event_location(
        self,
        actor_user_id: int,
        event_id: int,
        location_or_url: str,
        *,
        now: datetime,
    ) -> Event:
        with self._lock:
            event = self._require_event_access(actor_user_id, event_id)
            event.location_or_url = location_or_url.strip()
            self._audit(
                actor_user_id,
                "event.location_updated",
                "event",
                str(event_id),
                {"location_or_url": event.location_or_url},
                now=now,
            )
            return event

    def create_organizer_event(
        self,
        actor_user_id: int,
        event: Event,
        *,
        slots: list[EventSlot],
        image_token: str | None,
        image_url: str | None,
        now: datetime,
    ) -> Event:
        with self._lock:
            self._require_event_creator(actor_user_id)
            event.booked_count = 0
            created = self.add_event(event, slots=[_reset_slot_counter(slot) for slot in slots])
            self.ensure_organizer_event(actor_user_id, created.id)
            self._set_event_image_without_access_check(
                actor_user_id,
                created.id,
                token=image_token,
                url=image_url,
                now=now,
            )
            self._audit(actor_user_id, "event.created", "event", str(created.id), now=now)
            return self.get_event(created.id) or created

    def replace_organizer_event(
        self,
        actor_user_id: int,
        event: Event,
        *,
        slots: list[EventSlot],
        image_token: str | None,
        image_url: str | None,
        now: datetime,
    ) -> Event:
        with self._lock:
            current = self._require_event_access(actor_user_id, event.id)
            active_registrations = self._active_event_registrations(event.id)
            if event.capacity_total < len(active_registrations):
                raise BotDomainError("Лимит мест нельзя сделать меньше числа активных записей")
            current_slots = self._event_slots(event.id)
            if active_registrations and not _slots_match_existing(current_slots, slots):
                raise BotDomainError("У мероприятия уже есть записи, поэтому слоты можно только оставить текущими")

            event.created_at = current.created_at
            event.booked_count = current.booked_count
            event.registration_closed = current.registration_closed
            event.slots = []
            self.events[event.id] = event

            if not active_registrations:
                for slot_id in [slot.id for slot in current_slots]:
                    self.slots.pop(slot_id, None)
                for slot in slots:
                    slot.id = self._next_id("slot", slot.id)
                    slot.event_id = event.id
                    slot.booked_count = 0
                    self.slots[slot.id] = slot
            self._set_event_image_without_access_check(
                actor_user_id,
                event.id,
                token=image_token,
                url=image_url,
                now=now,
            )
            self._audit(actor_user_id, "event.rebuilt", "event", str(event.id), now=now)
            return self.get_event(event.id) or event

    def list_events(self, *, starts_at_from: datetime | None = None) -> list[Event]:
        events = list(self.events.values())
        if starts_at_from is not None:
            events = [event for event in events if event.starts_at >= starts_at_from]
        for event in events:
            event.slots = self._event_slots(event.id)
            self._attach_event_image(event)
        return sorted(events, key=lambda item: item.starts_at)

    def delete_expired_events(self, *, expired_before: datetime) -> int:
        with self._lock:
            event_ids = [
                event.id
                for event in self.events.values()
                if event.starts_at <= expired_before
            ]
            for event_id in event_ids:
                self._delete_event_cascade(event_id)
            return len(event_ids)

    def set_event_image(
        self,
        actor_user_id: int,
        event_id: int,
        *,
        token: str | None,
        url: str | None,
        now: datetime,
    ) -> Event:
        with self._lock:
            event = self._require_event_access(actor_user_id, event_id)
            self._set_event_image_without_access_check(
                actor_user_id,
                event_id,
                token=token,
                url=url,
                now=now,
            )
            return event

    def set_pending_event_image(
        self,
        user_id: int,
        event_id: int,
        *,
        now: datetime,
    ) -> None:
        with self._lock:
            self._require_event_access(user_id, event_id)
            self.pending_event_images[user_id] = (event_id, now)

    def get_pending_event_image(self, user_id: int) -> int | None:
        pending = self.pending_event_images.get(user_id)
        return pending[0] if pending else None

    def clear_pending_event_image(self, user_id: int) -> None:
        with self._lock:
            self.pending_event_images.pop(user_id, None)

    def available_places(self, event_id: int, slot_id: int | None) -> int:
        event = self._require_event(event_id)
        event.slots = self._event_slots(event_id)
        if event.slots:
            if slot_id is None:
                return sum(self.available_places(event_id, slot.id) for slot in event.slots)
            slot = self._require_slot(event, slot_id)
            return max(slot.capacity - slot.booked_count, 0)
        return max(event.capacity_total - event.booked_count, 0)

    def create_registration(
        self,
        *,
        user_id: int,
        event_id: int,
        slot_id: int | None,
        now: datetime,
        code_generator: CodeGenerator,
        render_reminder: ReminderRenderer,
    ) -> Registration:
        with self._lock:
            if not self.has_profile_consent(user_id):
                raise ConsentRequiredError("Нужно согласие на минимальные данные профиля")
            event = self._require_event(event_id)
            event.slots = self._event_slots(event_id)
            if event.registration_closed or event.starts_at <= now:
                raise RegistrationClosedError("Регистрация на мероприятие закрыта")
            if event.slots and slot_id is None:
                raise SlotRequiredError("Для мероприятия нужно выбрать слот")
            if slot_id is not None:
                self._require_slot(event, slot_id)
            active_key = self._active_key(user_id, event_id)
            if active_key in self.active_registration_keys:
                raise DuplicateActiveRegistrationError("Активная запись уже есть")
            if self.available_places(event_id, slot_id) <= 0:
                raise NoSeatsAvailableError("Свободных мест нет")

            code = self._next_unique_code(code_generator)
            registration = Registration(
                id=self._next_id("registration"),
                code=code,
                user_id=user_id,
                event_id=event_id,
                slot_id=slot_id,
                status=RegistrationStatus.CONFIRMED,
                notifications_enabled=True,
                created_at=now,
                updated_at=now,
            )
            self.registrations[registration.id] = registration
            self.registration_codes[code] = registration.id
            self.active_registration_keys[active_key] = registration.id
            self._increase_booked_count(event, slot_id)
            self._schedule_reminders(registration, event, now, render_reminder)
            self._audit(user_id, "registration.created", "registration", str(registration.id), now=now)
            return self._attach_registration(registration)

    def cancel_registration(
        self,
        *,
        user_id: int,
        registration_id: int,
        now: datetime,
    ) -> Registration:
        with self._lock:
            registration = self._require_registration(registration_id)
            if registration.user_id != user_id:
                raise RegistrationNotFoundError("Запись не найдена")
            if registration.status not in ACTIVE_REGISTRATION_STATUSES:
                return self._attach_registration(registration)
            event = self._require_event(registration.event_id)
            if event.starts_at <= now:
                if event.late_cancel_policy == LateCancelPolicy.DENY:
                    raise LateCancellationDeniedError("После начала мероприятия отмена недоступна")
                registration.status = RegistrationStatus.LATE_CANCELED
            else:
                registration.status = RegistrationStatus.CANCELED_BY_USER
            registration.canceled_at = now
            registration.updated_at = now
            self._remove_active_registration(registration)
            self._decrease_booked_count(registration)
            self._audit(user_id, "registration.cancelled", "registration", str(registration.id), now=now)
            return self._attach_registration(registration)

    def set_notifications_enabled(
        self,
        *,
        user_id: int,
        registration_id: int,
        enabled: bool,
        now: datetime,
    ) -> Registration:
        with self._lock:
            registration = self._require_registration(registration_id)
            if registration.user_id != user_id:
                raise RegistrationNotFoundError("Запись не найдена")
            registration.notifications_enabled = enabled
            registration.updated_at = now
            self._audit(
                user_id,
                "registration.notifications_changed",
                "registration",
                str(registration.id),
                {"enabled": enabled},
                now=now,
            )
            return self._attach_registration(registration)

    def get_registration(self, registration_id: int) -> Registration | None:
        registration = self.registrations.get(registration_id)
        return self._attach_registration(registration) if registration else None

    def list_user_registrations(self, user_id: int) -> list[Registration]:
        return sorted(
            [
                self._attach_registration(item)
                for item in self.registrations.values()
                if item.user_id == user_id
            ],
            key=lambda item: item.created_at,
            reverse=True,
        )

    def ensure_role(self, user_id: int, role: str) -> None:
        with self._lock:
            if any(item.user_id == user_id and item.role == role for item in self.roles.values()):
                return
            item = RoleAssignment(self._next_id("role"), user_id=user_id, role=role)
            self.roles[item.id] = item

    def has_role(self, user_id: int, role: str) -> bool:
        return any(item.user_id == user_id and item.role == role for item in self.roles.values())

    def ensure_organizer_event(self, user_id: int, event_id: int) -> None:
        with self._lock:
            if any(
                item.user_id == user_id and item.event_id == event_id
                for item in self.organizer_events.values()
            ):
                return
            item = OrganizerEvent(
                self._next_id("organizer_event"),
                user_id=user_id,
                event_id=event_id,
            )
            self.organizer_events[item.id] = item

    def list_organizer_events(self, actor_user_id: int) -> list[Event]:
        if self._is_admin(actor_user_id):
            return self.list_events()
        event_ids = {
            item.event_id
            for item in self.organizer_events.values()
            if item.user_id == actor_user_id
        }
        events = [self.get_event(event_id) for event_id in event_ids]
        return sorted([event for event in events if event is not None], key=lambda item: item.starts_at)

    def set_organizer_state(self, state: OrganizerState) -> OrganizerState:
        with self._lock:
            self.organizer_states[state.user_id] = state
            return state

    def get_organizer_state(self, user_id: int) -> OrganizerState | None:
        return self.organizer_states.get(user_id)

    def clear_organizer_state(self, user_id: int) -> None:
        with self._lock:
            self.organizer_states.pop(user_id, None)

    def get_event_registrations(
        self,
        actor_user_id: int,
        event_id: int,
    ) -> list[Registration]:
        self._require_event_access(actor_user_id, event_id)
        return sorted(
            [
                self._attach_registration(item)
                for item in self.registrations.values()
                if item.event_id == event_id
            ],
            key=lambda item: item.created_at,
            reverse=True,
        )

    def find_registration_by_code(
        self,
        actor_user_id: int,
        event_id: int,
        code: str,
    ) -> Registration:
        self._require_event_access(actor_user_id, event_id)
        normalized = code.strip().upper()
        for registration in self.registrations.values():
            if registration.event_id == event_id and registration.code == normalized:
                return self._attach_registration(registration)
        raise RegistrationNotFoundError("Запись не найдена")

    def find_registration_by_code_any_event(
        self,
        actor_user_id: int,
        code: str,
    ) -> Registration:
        event_ids = {event.id for event in self.list_organizer_events(actor_user_id)}
        if not event_ids:
            raise AccessDeniedError("Нет доступных мероприятий")
        normalized = code.strip().upper()
        for registration in self.registrations.values():
            if registration.event_id in event_ids and registration.code == normalized:
                return self._attach_registration(registration)
        raise RegistrationNotFoundError("Запись не найдена")

    def find_registration_by_code_global(self, code: str) -> Registration | None:
        registration_id = self.registration_codes.get(code.strip().upper())
        if registration_id is None:
            return None
        return self.get_registration(registration_id)

    def close_registration(self, actor_user_id: int, event_id: int) -> Event:
        with self._lock:
            event = self._require_event_access(actor_user_id, event_id)
            event.registration_closed = True
            self._audit(actor_user_id, "event.registration_closed", "event", str(event_id), now=utc_now())
            return event

    def mark_attended(
        self,
        actor_user_id: int,
        registration_id: int,
        *,
        now: datetime,
    ) -> Registration:
        with self._lock:
            registration = self._get_registration_for_actor(actor_user_id, registration_id)
            registration.status = RegistrationStatus.ATTENDED
            registration.attended_at = now
            registration.updated_at = now
            self._audit(actor_user_id, "registration.attended", "registration", str(registration_id), now=now)
            return self._attach_registration(registration)

    def change_status(
        self,
        actor_user_id: int,
        registration_id: int,
        status: RegistrationStatus,
        *,
        now: datetime,
    ) -> Registration:
        with self._lock:
            registration = self._get_registration_for_actor(actor_user_id, registration_id)
            was_active = registration.status in ACTIVE_REGISTRATION_STATUSES
            registration.status = status
            registration.updated_at = now
            if status not in ACTIVE_REGISTRATION_STATUSES:
                registration.canceled_at = now
            if was_active and status not in ACTIVE_REGISTRATION_STATUSES:
                self._remove_active_registration(registration)
                self._decrease_booked_count(registration)
            self._audit(
                actor_user_id,
                "registration.status_changed",
                "registration",
                str(registration_id),
                {"status": status.value},
                now=now,
            )
            return self._attach_registration(registration)

    def enqueue_manual_notification(
        self,
        *,
        actor_user_id: int,
        event_id: int,
        kind: NotificationKind,
        message_text: str,
        now: datetime,
    ) -> list[NotificationOutbox]:
        with self._lock:
            self._require_event_access(actor_user_id, event_id)
            if kind not in MANUAL_NOTIFICATION_KINDS:
                raise InvalidNotificationKindError("Этот тип уведомления нельзя отправить вручную")
            created: list[NotificationOutbox] = []
            for registration in self.registrations.values():
                if (
                    registration.event_id == event_id
                    and registration.status in ACTIVE_REGISTRATION_STATUSES
                    and registration.notifications_enabled
                ):
                    created.append(
                        self.add_notification(
                            NotificationOutbox(
                                id=0,
                                event_id=event_id,
                                registration_id=registration.id,
                                user_id=registration.user_id,
                                kind=kind,
                                message_text=message_text,
                                send_after=now,
                                created_at=now,
                            )
                        )
                    )
            self._audit(
                actor_user_id,
                "event.notification_enqueued",
                "event",
                str(event_id),
                {"kind": kind.value, "count": len(created)},
                now=now,
            )
            return created

    def add_notification(self, item: NotificationOutbox) -> NotificationOutbox:
        with self._lock:
            item.id = self._next_id("notification", item.id)
            self.notifications[item.id] = item
            return item

    def list_notifications(self) -> list[NotificationOutbox]:
        return sorted(self.notifications.values(), key=lambda item: item.id)

    def list_due_notifications(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[NotificationOutbox]:
        due = [
            item
            for item in self.notifications.values()
            if item.status == OutboxStatus.PENDING and item.send_after <= now
        ]
        due.sort(key=lambda item: (item.send_after, item.id))
        for item in due[:limit]:
            if item.registration_id is not None:
                item.registration = self.get_registration(item.registration_id)
        return due[:limit]

    def set_notification_result(
        self,
        notification_id: int,
        *,
        status: OutboxStatus,
        now: datetime,
        error: str | None = None,
    ) -> None:
        with self._lock:
            item = self.notifications[notification_id]
            item.status = status
            item.attempts += 1
            if status == OutboxStatus.SENT:
                item.sent_at = now
                item.last_error = None
            elif status == OutboxStatus.FAILED:
                item.last_error = (error or "")[:1000]

    def import_snapshot(self, snapshot) -> None:
        with self._lock:
            for user in snapshot.users:
                self.users[user.user_id] = user
            for event in snapshot.events:
                event.booked_count = 0
                slots = [slot for slot in snapshot.slots if slot.event_id == event.id]
                for slot in slots:
                    slot.booked_count = 0
                self.add_event(event, slots=slots)
            for consent in snapshot.consents:
                consent.id = self._next_id("consent", consent.id)
                self.consents[consent.id] = consent
            for role in snapshot.roles:
                role.id = self._next_id("role", role.id)
                self.roles[role.id] = role
            for organizer_event in snapshot.organizer_events:
                organizer_event.id = self._next_id("organizer_event", organizer_event.id)
                self.organizer_events[organizer_event.id] = organizer_event
            for registration in snapshot.registrations:
                registration.id = self._next_id("registration", registration.id)
                self.registrations[registration.id] = registration
                self.registration_codes[registration.code] = registration.id
                if registration.status in ACTIVE_REGISTRATION_STATUSES:
                    self.active_registration_keys[self._active_key(registration.user_id, registration.event_id)] = registration.id
                    self._increase_booked_count(self._require_event(registration.event_id), registration.slot_id)
            for notification in snapshot.notifications:
                self.add_notification(notification)
            for audit in snapshot.audit_logs:
                audit.id = self._next_id("audit", audit.id)
                self.audit_log[audit.id] = audit

    def _next_unique_code(self, code_generator: CodeGenerator) -> str:
        for _ in range(20):
            code = code_generator().strip().upper()
            if code not in self.registration_codes:
                return code
        raise RuntimeError("Не удалось сгенерировать уникальный код записи")

    def _schedule_reminders(
        self,
        registration: Registration,
        event: Event,
        now: datetime,
        render_reminder: ReminderRenderer,
    ) -> None:
        reminders = [
            (NotificationKind.REMINDER_24H, event.starts_at - timedelta(days=1)),
            (NotificationKind.REMINDER_1H, event.starts_at - timedelta(hours=1)),
        ]
        for kind, send_after in reminders:
            if send_after > now:
                self.add_notification(
                    NotificationOutbox(
                        id=0,
                        event_id=event.id,
                        registration_id=registration.id,
                        user_id=registration.user_id,
                        kind=kind,
                        message_text=render_reminder(kind, event, registration),
                        send_after=send_after,
                        created_at=now,
                    )
                )

    def _ensure_user_exists(self, user_id: int, *, now: datetime) -> None:
        if user_id not in self.users:
            self.upsert_user(user_id, f"Пользователь {user_id}", now=now)

    def _event_slots(self, event_id: int) -> list[EventSlot]:
        return sorted(
            [slot for slot in self.slots.values() if slot.event_id == event_id],
            key=lambda item: item.starts_at,
        )

    def _require_event(self, event_id: int) -> Event:
        event = self.events.get(event_id)
        if event is None:
            raise EventNotFoundError("Мероприятие не найдено")
        return event

    def _require_slot(self, event: Event, slot_id: int) -> EventSlot:
        slot = self.slots.get(slot_id)
        if slot is None or slot.event_id != event.id:
            raise SlotNotFoundError("Слот не найден")
        return slot

    def _require_registration(self, registration_id: int) -> Registration:
        registration = self.registrations.get(registration_id)
        if registration is None:
            raise RegistrationNotFoundError("Запись не найдена")
        return registration

    def _is_admin(self, user_id: int) -> bool:
        return any(item.user_id == user_id and item.role == "admin" for item in self.roles.values())

    def _has_event_access(self, user_id: int, event_id: int) -> bool:
        if self._is_admin(user_id):
            return True
        return any(
            item.user_id == user_id and item.event_id == event_id
            for item in self.organizer_events.values()
        )

    def _require_event_access(self, user_id: int, event_id: int) -> Event:
        event = self._require_event(event_id)
        if not self._has_event_access(user_id, event_id):
            raise AccessDeniedError("Нет доступа к этому мероприятию")
        event.slots = self._event_slots(event.id)
        self._attach_event_image(event)
        return event

    def _require_event_creator(self, user_id: int) -> None:
        if self._is_admin(user_id) or self.has_role(user_id, "organizer"):
            return
        raise AccessDeniedError("Нет доступа к созданию мероприятий")

    def _active_event_registrations(self, event_id: int) -> list[Registration]:
        return [
            registration
            for registration in self.registrations.values()
            if registration.event_id == event_id
            and registration.status in ACTIVE_REGISTRATION_STATUSES
        ]

    def _set_event_image_without_access_check(
        self,
        actor_user_id: int,
        event_id: int,
        *,
        token: str | None,
        url: str | None,
        now: datetime,
    ) -> None:
        event = self._require_event(event_id)
        clean_token = (token or "").strip() or None
        clean_url = (url or "").strip() or None
        self.event_images[event_id] = (clean_token, clean_url, actor_user_id, now)
        event.image_token = clean_token
        event.image_url = clean_url
        self._audit(
            actor_user_id,
            "event.image_updated",
            "event",
            str(event_id),
            {"has_token": clean_token is not None, "has_url": clean_url is not None},
            now=now,
        )

    def _get_registration_for_actor(self, actor_user_id: int, registration_id: int) -> Registration:
        registration = self._require_registration(registration_id)
        self._require_event_access(actor_user_id, registration.event_id)
        return registration

    def _increase_booked_count(self, event: Event, slot_id: int | None) -> None:
        if slot_id is not None:
            self.slots[slot_id].booked_count += 1
        else:
            event.booked_count += 1

    def _decrease_booked_count(self, registration: Registration) -> None:
        if registration.slot_id is not None and registration.slot_id in self.slots:
            self.slots[registration.slot_id].booked_count = max(
                self.slots[registration.slot_id].booked_count - 1,
                0,
            )
        elif registration.event_id in self.events:
            self.events[registration.event_id].booked_count = max(
                self.events[registration.event_id].booked_count - 1,
                0,
            )

    def _remove_active_registration(self, registration: Registration) -> None:
        self.active_registration_keys.pop(
            self._active_key(registration.user_id, registration.event_id),
            None,
        )

    def _attach_registration(self, registration: Registration) -> Registration:
        registration.event = self.get_event(registration.event_id)
        registration.slot = self.slots.get(registration.slot_id) if registration.slot_id else None
        registration.user = self.users.get(registration.user_id)
        return registration

    def _attach_event_image(self, event: Event) -> Event:
        image = self.event_images.get(event.id)
        if image is None:
            event.image_token = None
            event.image_url = None
            return event
        event.image_token = image[0]
        event.image_url = image[1]
        return event

    def _delete_event_cascade(self, event_id: int) -> None:
        registration_ids = {
            registration.id
            for registration in self.registrations.values()
            if registration.event_id == event_id
        }
        registration_id_texts = {str(registration_id) for registration_id in registration_ids}

        for registration_id in list(registration_ids):
            registration = self.registrations.pop(registration_id, None)
            if registration is None:
                continue
            self.registration_codes.pop(registration.code, None)
            self.active_registration_keys.pop(
                self._active_key(registration.user_id, registration.event_id),
                None,
            )

        for slot_id, slot in list(self.slots.items()):
            if slot.event_id == event_id:
                self.slots.pop(slot_id, None)
        for organizer_event_id, organizer_event in list(self.organizer_events.items()):
            if organizer_event.event_id == event_id:
                self.organizer_events.pop(organizer_event_id, None)
        for notification_id, notification in list(self.notifications.items()):
            if notification.event_id == event_id or notification.registration_id in registration_ids:
                self.notifications.pop(notification_id, None)
        for slug, linked_event_id in list(self.event_deeplinks.items()):
            if linked_event_id == event_id:
                self.event_deeplinks.pop(slug, None)
        self.event_images.pop(event_id, None)
        for user_id, pending in list(self.pending_event_images.items()):
            if pending[0] == event_id:
                self.pending_event_images.pop(user_id, None)
        for user_id, state in list(self.organizer_states.items()):
            if state.event_id == event_id:
                self.organizer_states.pop(user_id, None)
        for audit_id, audit in list(self.audit_log.items()):
            if (
                audit.entity_type == "event"
                and audit.entity_id == str(event_id)
                or audit.entity_type == "registration"
                and audit.entity_id in registration_id_texts
            ):
                self.audit_log.pop(audit_id, None)
        self.events.pop(event_id, None)

    @staticmethod
    def _active_key(user_id: int, event_id: int) -> str:
        return f"{user_id}:{event_id}"

    def _audit(
        self,
        actor_user_id: int | None,
        action: str,
        entity_type: str,
        entity_id: str,
        metadata: dict | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(timezone.utc)
        item = AuditLog(
            id=self._next_id("audit"),
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_json=metadata or {},
            created_at=current,
        )
        self.audit_log[item.id] = item


def _reset_slot_counter(slot: EventSlot) -> EventSlot:
    slot.booked_count = 0
    return slot


def _slots_match_existing(current: list[EventSlot], incoming: list[EventSlot]) -> bool:
    if len(current) != len(incoming):
        return False
    for existing, candidate in zip(current, incoming, strict=True):
        if candidate.id != existing.id:
            return False
        if candidate.title != existing.title:
            return False
        if candidate.starts_at != existing.starts_at:
            return False
        if candidate.ends_at != existing.ends_at:
            return False
        if candidate.capacity != existing.capacity:
            return False
    return True
