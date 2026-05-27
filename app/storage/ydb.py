from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone

import ydb

from app.observability.performance import record_method
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
    EventFormat,
    LateCancelPolicy,
    NotificationKind,
    OutboxStatus,
    RegistrationStatus,
)
from app.services.reminders import (
    LEGACY_AUTOMATIC_REMINDER_KINDS,
    automatic_reminder_schedule,
)
from app.services.registration_codes import normalize_registration_code_input
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


class YdbStorage:
    def __init__(
        self,
        *,
        endpoint: str,
        database: str,
        use_metadata_credentials: bool = False,
        pool_size: int = 20,
    ) -> None:
        credentials = self._credentials(use_metadata_credentials)
        self.driver = ydb.Driver(
            endpoint=endpoint,
            database=database,
            credentials=credentials,
        )
        self.driver.wait(timeout=10, fail_fast=True)
        self.pool = ydb.QuerySessionPool(self.driver, size=pool_size)

    def ready(self) -> bool:
        try:
            self._query("SELECT 1 AS ok;")
        except Exception:
            return False
        return True

    def upsert_user(
        self,
        user_id: int,
        display_name: str,
        *,
        is_bot: bool = False,
        now: datetime | None = None,
    ) -> User:
        current = _dt(now or utc_now())
        existing = self.get_user(user_id)
        created_at = existing.created_at if existing else current
        user = User(
            user_id=user_id,
            display_name=display_name or (existing.display_name if existing else f"Пользователь {user_id}"),
            is_bot=is_bot,
            created_at=created_at,
            updated_at=current,
        )
        self._execute(
            """
            DECLARE $user_id AS Int64;
            DECLARE $display_name AS Utf8;
            DECLARE $is_bot AS Bool;
            DECLARE $created_at AS Timestamp;
            DECLARE $updated_at AS Timestamp;
            UPSERT INTO users (user_id, display_name, is_bot, created_at, updated_at)
            VALUES ($user_id, $display_name, $is_bot, $created_at, $updated_at);
            """,
            {
                "$user_id": _int(user.user_id),
                "$display_name": _utf8(user.display_name),
                "$is_bot": _bool(user.is_bot),
                "$created_at": _timestamp(user.created_at),
                "$updated_at": _timestamp(user.updated_at),
            },
        )
        return user

    def touch_user(
        self,
        user_id: int,
        display_name: str,
        *,
        is_bot: bool = False,
        now: datetime | None = None,
    ) -> None:
        current = _dt(now or utc_now())
        clean_display_name = (display_name or "").strip()
        self._execute(
            """
            DECLARE $user_id AS Int64;
            DECLARE $display_name AS Utf8;
            DECLARE $default_display_name AS Utf8;
            DECLARE $is_bot AS Bool;
            DECLARE $updated_at AS Timestamp;

            UPDATE users
            SET
                display_name = CASE
                    WHEN $display_name != "" THEN $display_name
                    ELSE display_name
                END,
                is_bot = $is_bot,
                updated_at = $updated_at
            WHERE user_id = $user_id;

            INSERT INTO users (
                user_id, display_name, is_bot, created_at, updated_at
            )
            SELECT
                new_user.user_id AS user_id,
                new_user.display_name AS display_name,
                new_user.is_bot AS is_bot,
                new_user.updated_at AS created_at,
                new_user.updated_at AS updated_at
            FROM (
                SELECT
                    $user_id AS user_id,
                    CASE
                        WHEN $display_name != "" THEN $display_name
                        ELSE $default_display_name
                    END AS display_name,
                    $is_bot AS is_bot,
                    $updated_at AS updated_at
            ) AS new_user
            WHERE NOT EXISTS (
                SELECT user_id FROM users WHERE user_id = $user_id
            );
            """,
            {
                "$user_id": _int(user_id),
                "$display_name": _utf8(clean_display_name),
                "$default_display_name": _utf8(f"Пользователь {user_id}"),
                "$is_bot": _bool(is_bot),
                "$updated_at": _timestamp(current),
            },
        )

    def get_user(self, user_id: int) -> User | None:
        row = self._one(
            """
            DECLARE $user_id AS Int64;
            SELECT * FROM users WHERE user_id = $user_id;
            """,
            {"$user_id": _int(user_id)},
        )
        return _user(row) if row else None

    def record_profile_consent(
        self,
        user_id: int,
        document_version: str,
        *,
        now: datetime,
    ) -> Consent:
        current = _dt(now)
        self.upsert_user(user_id, f"Пользователь {user_id}", now=current)
        consent = Consent(
            id=self._new_id("consents"),
            user_id=user_id,
            document_version=document_version,
            profile_data_allowed=True,
            created_at=current,
        )
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $user_id AS Int64;
            DECLARE $document_version AS Utf8;
            DECLARE $profile_data_allowed AS Bool;
            DECLARE $created_at AS Timestamp;
            UPSERT INTO consents (id, user_id, document_version, profile_data_allowed, created_at)
            VALUES ($id, $user_id, $document_version, $profile_data_allowed, $created_at);
            """,
            _consent_params(consent),
        )
        self._audit(user_id, "consent.accepted", "user", str(user_id), now=current)
        return consent

    def has_profile_consent(self, user_id: int) -> bool:
        row = self._one(
            """
            DECLARE $user_id AS Int64;
            SELECT id FROM consents VIEW idx_consents_user
            WHERE user_id = $user_id AND profile_data_allowed = true
            LIMIT 1;
            """,
            {"$user_id": _int(user_id)},
        )
        return row is not None

    def add_event(self, event: Event, *, slots: list[EventSlot] | None = None) -> Event:
        if not event.id:
            event.id = self._new_id("events")
        event.booked_count = event.booked_count or 0
        self._execute(_UPSERT_EVENT, _event_params(event))
        event.slots = []
        for slot in slots or []:
            if not slot.id:
                slot.id = self._new_id("event_slots")
            slot.event_id = event.id
            slot.booked_count = slot.booked_count or 0
            self._execute(_UPSERT_SLOT, _slot_params(slot))
            event.slots.append(slot)
        return event

    def get_event(
        self,
        event_id: int,
        *,
        with_slots: bool = True,
        with_image: bool = True,
    ) -> Event | None:
        row = self._one(
            """
            DECLARE $id AS Int64;
            SELECT * FROM events WHERE id = $id;
            """,
            {"$id": _int(event_id)},
        )
        if row is None:
            return None
        event = _event(row)
        if with_slots:
            event.slots = self._event_slots_for_events([event.id]).get(event.id, [])
        if with_image:
            self._attach_event_image(event)
        return event

    def get_organizer_event(
        self,
        actor_user_id: int,
        event_id: int,
        *,
        with_slots: bool = True,
        with_image: bool = True,
    ) -> Event:
        if not self._has_event_access(actor_user_id, event_id):
            raise AccessDeniedError("Нет доступа к этому мероприятию")
        event = self.get_event(event_id, with_slots=with_slots, with_image=with_image)
        if event is None:
            raise EventNotFoundError("Мероприятие не найдено")
        return event

    def assign_event_slug(
        self,
        event_id: int,
        slug: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self._require_event(event_id)
        existing = self._one(
            """
            DECLARE $slug AS Utf8;
            SELECT event_id FROM event_deeplinks WHERE slug = $slug;
            """,
            {"$slug": _utf8(slug)},
        )
        if existing is not None:
            existing_event_id = int(existing.get("event_id") or 0)
            if existing_event_id == event_id:
                return
            raise DuplicateEventSlugError("Slug мероприятия уже используется")
        existing_slug = self.get_event_slug(event_id)
        if existing_slug is not None and existing_slug != slug:
            raise DuplicateEventSlugError("У мероприятия уже есть другой slug")
        self._execute(
            """
            DECLARE $slug AS Utf8;
            DECLARE $event_id AS Int64;
            DECLARE $created_at AS Timestamp;
            INSERT INTO event_deeplinks (slug, event_id, created_at)
            VALUES ($slug, $event_id, $created_at);
            """,
            {
                "$slug": _utf8(slug),
                "$event_id": _int(event_id),
                "$created_at": _timestamp(now or utc_now()),
            },
        )

    def get_event_by_slug(self, slug: str) -> Event | None:
        row = self._one(
            """
            DECLARE $slug AS Utf8;
            SELECT event_id FROM event_deeplinks WHERE slug = $slug;
            """,
            {"$slug": _utf8(slug)},
        )
        if row is None:
            return None
        return self.get_event(int(row.get("event_id") or 0))

    def get_event_slug(self, event_id: int) -> str | None:
        row = self._one(
            """
            DECLARE $event_id AS Int64;
            SELECT slug FROM event_deeplinks VIEW idx_event_deeplinks_event
            WHERE event_id = $event_id
            LIMIT 1;
            """,
            {"$event_id": _int(event_id)},
        )
        if row is None:
            return None
        slug = row.get("slug")
        return str(slug) if slug else None

    def update_event_start(self, event_id: int, starts_at: datetime) -> None:
        self._require_event(event_id)
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $starts_at AS Timestamp;
            UPDATE events SET starts_at = $starts_at WHERE id = $id;
            """,
            {"$id": _int(event_id), "$starts_at": _timestamp(starts_at)},
        )

    def reschedule_event(
        self,
        actor_user_id: int,
        event_id: int,
        starts_at: datetime,
        *,
        now: datetime,
    ) -> Event:
        self._require_event_access(actor_user_id, event_id)
        self.update_event_start(event_id, starts_at)
        self._audit(
            actor_user_id,
            "event.rescheduled",
            "event",
            str(event_id),
            {"starts_at": starts_at.isoformat()},
            now=now,
        )
        return self._require_event(event_id)

    def update_event_location(
        self,
        actor_user_id: int,
        event_id: int,
        location_or_url: str,
        *,
        now: datetime,
    ) -> Event:
        self._require_event_access(actor_user_id, event_id)
        clean_location = location_or_url.strip()
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $location_or_url AS Utf8;
            UPDATE events SET location_or_url = $location_or_url WHERE id = $id;
            """,
            {"$id": _int(event_id), "$location_or_url": _utf8(clean_location)},
        )
        self._audit(
            actor_user_id,
            "event.location_updated",
            "event",
            str(event_id),
            {"location_or_url": clean_location},
            now=now,
        )
        return self._require_event(event_id)

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
        return self._require_event(created.id)

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
        self._execute(_UPSERT_EVENT, _event_params(event))

        if not active_registrations:
            self._execute(
                """
                DECLARE $event_id AS Int64;
                DELETE FROM event_slots WHERE event_id = $event_id;
                """,
                {"$event_id": _int(event.id)},
            )
            for slot in slots:
                if not slot.id:
                    slot.id = self._new_id("event_slots")
                slot.event_id = event.id
                slot.booked_count = 0
                self._execute(_UPSERT_SLOT, _slot_params(slot))
        self._set_event_image_without_access_check(
            actor_user_id,
            event.id,
            token=image_token,
            url=image_url,
            now=now,
        )
        self._audit(actor_user_id, "event.rebuilt", "event", str(event.id), now=now)
        return self._require_event(event.id)

    def set_event_image(
        self,
        actor_user_id: int,
        event_id: int,
        *,
        token: str | None,
        url: str | None,
        now: datetime,
    ) -> Event:
        self._require_event_access(actor_user_id, event_id)
        self._set_event_image_without_access_check(
            actor_user_id,
            event_id,
            token=token,
            url=url,
            now=now,
        )
        return self._require_event(event_id)

    def set_pending_event_image(
        self,
        user_id: int,
        event_id: int,
        *,
        now: datetime,
    ) -> None:
        self._require_event_access(user_id, event_id)
        self._execute(
            """
            DECLARE $user_id AS Int64;
            DECLARE $event_id AS Int64;
            DECLARE $created_at AS Timestamp;
            UPSERT INTO pending_event_images (user_id, event_id, created_at)
            VALUES ($user_id, $event_id, $created_at);
            """,
            {
                "$user_id": _int(user_id),
                "$event_id": _int(event_id),
                "$created_at": _timestamp(now),
            },
        )

    def get_pending_event_image(self, user_id: int) -> int | None:
        row = self._one(
            """
            DECLARE $user_id AS Int64;
            SELECT event_id FROM pending_event_images WHERE user_id = $user_id;
            """,
            {"$user_id": _int(user_id)},
        )
        if row is None:
            return None
        event_id = row.get("event_id")
        return int(event_id) if event_id is not None else None

    def clear_pending_event_image(self, user_id: int) -> None:
        self._execute(
            """
            DECLARE $user_id AS Int64;
            DELETE FROM pending_event_images WHERE user_id = $user_id;
            """,
            {"$user_id": _int(user_id)},
        )

    def clear_user_draft_state(self, user_id: int) -> None:
        self._execute(
            """
            DECLARE $user_id AS Int64;
            DELETE FROM organizer_states WHERE user_id = $user_id;
            DELETE FROM pending_event_images WHERE user_id = $user_id;
            """,
            {"$user_id": _int(user_id)},
        )

    def list_events(
        self,
        *,
        starts_at_from: datetime | None = None,
        with_slots: bool = True,
        with_images: bool = True,
    ) -> list[Event]:
        if starts_at_from is None:
            rows = self._query("SELECT * FROM events VIEW idx_events_starts_at ORDER BY starts_at;")
        else:
            rows = self._query(
                """
                DECLARE $starts_at_from AS Timestamp;
                SELECT * FROM events VIEW idx_events_starts_at
                WHERE starts_at >= $starts_at_from
                ORDER BY starts_at;
                """,
                {"$starts_at_from": _timestamp(starts_at_from)},
            )
        events = [_event(row) for row in rows]
        if with_slots:
            slots_by_event = self._event_slots_for_events([event.id for event in events])
            for event in events:
                event.slots = slots_by_event.get(event.id, [])
        if with_images:
            self._attach_event_images_for_events(events)
        return events

    def delete_expired_events(self, *, expired_before: datetime) -> int:
        rows = self._query(
            """
            DECLARE $expired_before AS Timestamp;
            SELECT id FROM events VIEW idx_events_starts_at
            WHERE starts_at <= $expired_before;
            """,
            {"$expired_before": _timestamp(expired_before)},
        )
        event_ids = [int(row["id"]) for row in rows]
        for event_id in event_ids:
            self._delete_event_cascade(event_id)
        return len(event_ids)

    def available_places(self, event_id: int, slot_id: int | None) -> int:
        event = self._require_event(event_id)
        if event.slots:
            if slot_id is None:
                return sum(
                    max(slot.capacity - slot.booked_count, 0)
                    for slot in event.slots
                )
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
        current = _dt(now)

        def callee(session):
            with session.transaction(tx_mode=ydb.QuerySerializableReadWrite()) as tx:
                if not self._tx_has_consent(tx, user_id):
                    raise ConsentRequiredError("Нужно согласие на минимальные данные профиля")
                event = self._tx_event(tx, event_id)
                if event is None:
                    raise EventNotFoundError("Мероприятие не найдено")
                slots = self._tx_event_slots(tx, event_id)
                event.slots = slots
                if event.registration_closed or event.starts_at <= current:
                    raise RegistrationClosedError("Регистрация на мероприятие закрыта")
                if slots and slot_id is None:
                    raise SlotRequiredError("Для мероприятия нужно выбрать слот")
                selected_slot = None
                if slot_id is not None:
                    selected_slot = next((slot for slot in slots if slot.id == slot_id), None)
                    if selected_slot is None:
                        raise SlotNotFoundError("Слот не найден")
                active_key = self._active_key(user_id, event_id)
                if self._tx_active_key_exists(tx, active_key):
                    raise DuplicateActiveRegistrationError("Активная запись уже есть")
                if self._tx_available_places(event, slots, slot_id) <= 0:
                    raise NoSeatsAvailableError("Свободных мест нет")

                registration = Registration(
                    id=self._new_id("registrations"),
                    code=self._tx_next_unique_code(tx, code_generator),
                    user_id=user_id,
                    event_id=event_id,
                    slot_id=slot_id,
                    status=RegistrationStatus.CONFIRMED,
                    notifications_enabled=True,
                    created_at=current,
                    updated_at=current,
                )
                self._tx_execute(tx, _UPSERT_REGISTRATION, _registration_params(registration))
                self._tx_execute(
                    tx,
                    """
                    DECLARE $code AS Utf8;
                    DECLARE $registration_id AS Int64;
                    INSERT INTO registration_codes (code, registration_id)
                    VALUES ($code, $registration_id);
                    """,
                    {"$code": _utf8(registration.code), "$registration_id": _int(registration.id)},
                )
                self._tx_execute(
                    tx,
                    """
                    DECLARE $active_key AS Utf8;
                    DECLARE $registration_id AS Int64;
                    INSERT INTO active_registration_keys (active_key, registration_id)
                    VALUES ($active_key, $registration_id);
                    """,
                    {"$active_key": _utf8(active_key), "$registration_id": _int(registration.id)},
                )
                if slot_id is None:
                    self._tx_execute(
                        tx,
                        """
                        DECLARE $id AS Int64;
                        UPDATE events SET booked_count = booked_count + 1 WHERE id = $id;
                        """,
                        {"$id": _int(event_id)},
                    )
                    event.booked_count += 1
                else:
                    self._tx_execute(
                        tx,
                        """
                        DECLARE $id AS Int64;
                        UPDATE event_slots SET booked_count = booked_count + 1 WHERE id = $id;
                        """,
                        {"$id": _int(slot_id)},
                    )
                    if selected_slot is not None:
                        selected_slot.booked_count += 1
                self._tx_schedule_reminders(tx, registration, event, current, render_reminder)
                self._tx_audit(tx, user_id, "registration.created", "registration", str(registration.id), now=current)
                self._tx_execute(tx, "SELECT 1;", commit=True)
                registration.event = event
                registration.slot = selected_slot
                return registration

        return self.pool.retry_operation_sync(
            callee,
            retry_settings=ydb.RetrySettings(max_retries=10, idempotent=False),
        )

    def cancel_registration(
        self,
        *,
        user_id: int,
        registration_id: int,
        now: datetime,
    ) -> Registration:
        current = _dt(now)

        def callee(session):
            with session.transaction(tx_mode=ydb.QuerySerializableReadWrite()) as tx:
                registration = self._tx_registration(tx, registration_id)
                if registration is None or registration.user_id != user_id:
                    raise RegistrationNotFoundError("Запись не найдена")
                if registration.status not in ACTIVE_REGISTRATION_STATUSES:
                    self._tx_execute(tx, "SELECT 1;", commit=True)
                    return registration.id
                event = self._tx_event(tx, registration.event_id)
                if event is None:
                    raise EventNotFoundError("Мероприятие не найдено")
                if event.starts_at <= current:
                    if event.late_cancel_policy == LateCancelPolicy.DENY:
                        raise LateCancellationDeniedError("После начала мероприятия отмена недоступна")
                    status = RegistrationStatus.LATE_CANCELED
                    self._tx_update_registration_status(tx, registration, status, current, canceled_at=current)
                    self._tx_remove_active_registration(tx, registration)
                    self._tx_decrease_booked_count(tx, registration)
                    self._tx_audit(tx, user_id, "registration.cancelled", "registration", str(registration.id), now=current)
                    self._tx_execute(tx, "SELECT 1;", commit=True)
                    return registration.id
                else:
                    status = RegistrationStatus.CANCELED_BY_USER
                    registration.status = status
                    registration.canceled_at = current
                    registration.updated_at = current
                registration.event = event
                self._tx_remove_active_registration(tx, registration)
                self._tx_decrease_booked_count(tx, registration)
                self._tx_audit(tx, user_id, "registration.cancelled", "registration", str(registration.id), now=current)
                self._tx_delete_registration_cascade(tx, registration)
                self._tx_execute(tx, "SELECT 1;", commit=True)
                return registration

        result_id = self.pool.retry_operation_sync(callee, retry_settings=ydb.RetrySettings(max_retries=10))
        if isinstance(result_id, Registration):
            return self._attach_registration(result_id)
        registration = self.get_registration(result_id)
        if registration is None:  # pragma: no cover - defensive edge
            raise RegistrationNotFoundError("Запись не найдена")
        return registration

    def set_notifications_enabled(
        self,
        *,
        user_id: int,
        registration_id: int,
        enabled: bool,
        now: datetime,
    ) -> Registration:
        registration = self._require_registration(registration_id)
        if registration.user_id != user_id:
            raise RegistrationNotFoundError("Запись не найдена")
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $enabled AS Bool;
            DECLARE $updated_at AS Timestamp;
            UPDATE registrations
            SET notifications_enabled = $enabled, updated_at = $updated_at
            WHERE id = $id;
            """,
            {
                "$id": _int(registration_id),
                "$enabled": _bool(enabled),
                "$updated_at": _timestamp(now),
            },
        )
        self._audit(
            user_id,
            "registration.notifications_changed",
            "registration",
            str(registration_id),
            {"enabled": enabled},
            now=now,
        )
        return self._require_registration(registration_id)

    def get_registration(self, registration_id: int) -> Registration | None:
        row = self._one(
            """
            DECLARE $id AS Int64;
            SELECT * FROM registrations WHERE id = $id;
            """,
            {"$id": _int(registration_id)},
        )
        return self._attach_registration(_registration(row)) if row else None

    def list_user_registrations(
        self,
        user_id: int,
        *,
        with_event_slots: bool = True,
        with_slot: bool = True,
        with_user: bool = True,
        with_images: bool = True,
    ) -> list[Registration]:
        rows = self._query(
            """
            DECLARE $user_id AS Int64;
            SELECT * FROM registrations VIEW idx_registrations_user
            WHERE user_id = $user_id
            ORDER BY created_at DESC;
            """,
            {"$user_id": _int(user_id)},
        )
        registrations = [_registration(row) for row in rows]
        self._attach_registrations_batch(
            registrations,
            with_event_slots=with_event_slots,
            with_slot=with_slot,
            with_user=with_user,
            with_images=with_images,
        )
        return registrations

    def get_active_registration_for_event(
        self,
        user_id: int,
        event_id: int,
    ) -> Registration | None:
        row = self._one(
            """
            DECLARE $active_key AS Utf8;
            SELECT registration_id FROM active_registration_keys
            WHERE active_key = $active_key;
            """,
            {"$active_key": _utf8(self._active_key(user_id, event_id))},
        )
        if row is None:
            return None
        registration_row = self._one(
            """
            DECLARE $id AS Int64;
            SELECT * FROM registrations WHERE id = $id;
            """,
            {"$id": _int(int(row.get("registration_id") or 0))},
        )
        registration = _registration(registration_row) if registration_row else None
        if registration is None or registration.status not in ACTIVE_REGISTRATION_STATUSES:
            return None
        return registration

    def delete_user_canceled_registrations(self) -> int:
        rows = self._query(
            """
            DECLARE $status AS Utf8;
            SELECT id, code, user_id, event_id FROM registrations
            WHERE status = $status;
            """,
            {"$status": _utf8(RegistrationStatus.CANCELED_BY_USER.value)},
        )
        for row in rows:
            registration = Registration(
                id=int(row["id"]),
                code=str(row["code"]),
                user_id=int(row["user_id"]),
                event_id=int(row["event_id"]),
                slot_id=None,
                status=RegistrationStatus.CANCELED_BY_USER,
            )
            self._delete_registration_cascade(registration)
        return len(rows)

    def ensure_role(
        self,
        user_id: int,
        role: str,
        *,
        created_at: datetime | None = None,
        created_by_user_id: int | None = None,
    ) -> RoleAssignment:
        existing = self.get_role(user_id, role)
        if existing is not None:
            return existing
        item = RoleAssignment(
            id=self._new_id("role_assignments"),
            user_id=user_id,
            role=role,
            created_at=created_at or utc_now(),
            created_by_user_id=created_by_user_id,
        )
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $user_id AS Int64;
            DECLARE $role AS Utf8;
            DECLARE $created_at AS Optional<Timestamp>;
            DECLARE $created_by_user_id AS Optional<Int64>;
            UPSERT INTO role_assignments (
                id, user_id, role, created_at, created_by_user_id
            )
            VALUES (
                $id, $user_id, $role, $created_at, $created_by_user_id
            );
            """,
            _role_params(item),
        )
        return item

    def get_role(self, user_id: int, role: str) -> RoleAssignment | None:
        row = self._one(
            """
            DECLARE $user_id AS Int64;
            DECLARE $role AS Utf8;
            SELECT * FROM role_assignments VIEW idx_roles_user
            WHERE user_id = $user_id AND role = $role
            LIMIT 1;
            """,
            {"$user_id": _int(user_id), "$role": _utf8(role)},
        )
        return _role(row) if row is not None else None

    def list_roles(self, role: str) -> list[RoleAssignment]:
        rows = self._query(
            """
            DECLARE $role AS Utf8;
            SELECT * FROM role_assignments
            WHERE role = $role
            ORDER BY created_at, user_id;
            """,
            {"$role": _utf8(role)},
        )
        return [_role(row) for row in rows]

    def has_role(self, user_id: int, role: str) -> bool:
        return self.get_role(user_id, role) is not None

    def get_user_roles(self, user_id: int) -> set[str]:
        rows = self._query(
            """
            DECLARE $user_id AS Int64;
            SELECT role FROM role_assignments VIEW idx_roles_user
            WHERE user_id = $user_id;
            """,
            {"$user_id": _int(user_id)},
        )
        return {str(row["role"]) for row in rows if row.get("role")}

    def delete_role(self, user_id: int, role: str) -> bool:
        item = self.get_role(user_id, role)
        if item is None:
            return False
        self._execute(
            """
            DECLARE $id AS Int64;
            DELETE FROM role_assignments WHERE id = $id;
            """,
            {"$id": _int(item.id)},
        )
        if role == "organizer":
            self._execute(
                """
                DECLARE $user_id AS Int64;
                DELETE FROM organizer_events WHERE user_id = $user_id;
                """,
                {"$user_id": _int(user_id)},
            )
        return True

    def ensure_organizer_event(self, user_id: int, event_id: int) -> None:
        row = self._one(
            """
            DECLARE $user_id AS Int64;
            DECLARE $event_id AS Int64;
            SELECT id FROM organizer_events VIEW idx_organizer_events_user
            WHERE user_id = $user_id AND event_id = $event_id
            LIMIT 1;
            """,
            {"$user_id": _int(user_id), "$event_id": _int(event_id)},
        )
        if row is not None:
            return
        item = OrganizerEvent(id=self._new_id("organizer_events"), user_id=user_id, event_id=event_id)
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $user_id AS Int64;
            DECLARE $event_id AS Int64;
            UPSERT INTO organizer_events (id, user_id, event_id)
            VALUES ($id, $user_id, $event_id);
            """,
            _organizer_event_params(item),
        )

    def list_organizer_events(
        self,
        actor_user_id: int,
        *,
        with_slots: bool = True,
        with_images: bool = True,
    ) -> list[Event]:
        if self._is_admin(actor_user_id):
            return self.list_events(with_slots=with_slots, with_images=with_images)
        rows = self._query(
            """
            DECLARE $user_id AS Int64;
            SELECT event_id FROM organizer_events VIEW idx_organizer_events_user
            WHERE user_id = $user_id;
            """,
            {"$user_id": _int(actor_user_id)},
        )
        events = self._events_by_ids(
            [int(row["event_id"]) for row in rows],
            with_slots=with_slots,
            with_images=with_images,
        )
        return sorted(events.values(), key=lambda item: item.starts_at)

    def set_organizer_state(self, state: OrganizerState) -> OrganizerState:
        self._execute(
            """
            DECLARE $user_id AS Int64;
            DECLARE $mode AS Utf8;
            DECLARE $event_id AS Optional<Int64>;
            DECLARE $step AS Utf8;
            DECLARE $data_json AS Utf8;
            DECLARE $updated_at AS Timestamp;
            UPSERT INTO organizer_states (
                user_id, mode, event_id, step, data_json, updated_at
            ) VALUES (
                $user_id, $mode, $event_id, $step, $data_json, $updated_at
            );
            """,
            _organizer_state_params(state),
        )
        return state

    def get_organizer_state(self, user_id: int) -> OrganizerState | None:
        row = self._one(
            """
            DECLARE $user_id AS Int64;
            SELECT * FROM organizer_states WHERE user_id = $user_id;
            """,
            {"$user_id": _int(user_id)},
        )
        if row is None:
            return None
        return _organizer_state(row)

    def clear_organizer_state(self, user_id: int) -> None:
        self._execute(
            """
            DECLARE $user_id AS Int64;
            DELETE FROM organizer_states WHERE user_id = $user_id;
            """,
            {"$user_id": _int(user_id)},
        )

    def get_event_registrations(
        self,
        actor_user_id: int,
        event_id: int,
        *,
        with_event: bool = True,
        with_event_slots: bool = True,
        with_slot: bool = True,
        with_user: bool = True,
        with_images: bool = True,
    ) -> list[Registration]:
        self._require_event_access(actor_user_id, event_id)
        rows = self._query(
            """
            DECLARE $event_id AS Int64;
            SELECT * FROM registrations VIEW idx_registrations_event
            WHERE event_id = $event_id
            ORDER BY created_at DESC;
            """,
            {"$event_id": _int(event_id)},
        )
        registrations = [_registration(row) for row in rows]
        self._attach_registrations_batch(
            registrations,
            with_event=with_event,
            with_event_slots=with_event_slots,
            with_slot=with_slot,
            with_user=with_user,
            with_images=with_images,
        )
        return registrations

    def find_registration_by_code(
        self,
        actor_user_id: int,
        event_id: int,
        code: str,
    ) -> Registration:
        self._require_event_access(actor_user_id, event_id)
        normalized = normalize_registration_code_input(code)
        if normalized is None:
            raise RegistrationNotFoundError("Запись не найдена")
        row = self._one(
            """
            DECLARE $event_id AS Int64;
            DECLARE $code AS Utf8;
            SELECT * FROM registrations VIEW idx_registrations_event
            WHERE event_id = $event_id AND code = $code
            LIMIT 1;
            """,
            {"$event_id": _int(event_id), "$code": _utf8(normalized)},
        )
        if row is None:
            raise RegistrationNotFoundError("Запись не найдена")
        return self._attach_registration(_registration(row))

    def find_registration_by_code_any_event(
        self,
        actor_user_id: int,
        code: str,
    ) -> Registration:
        event_ids = {event.id for event in self.list_organizer_events(actor_user_id)}
        if not event_ids:
            raise AccessDeniedError("Нет доступных мероприятий")
        registration = self.find_registration_by_code_global(code)
        if registration is None or registration.event_id not in event_ids:
            raise RegistrationNotFoundError("Запись не найдена")
        return registration

    def find_registration_by_code_global(self, code: str) -> Registration | None:
        normalized = normalize_registration_code_input(code)
        if normalized is None:
            return None
        row = self._one(
            """
            DECLARE $code AS Utf8;
            SELECT registration_id FROM registration_codes WHERE code = $code;
            """,
            {"$code": _utf8(normalized)},
        )
        if row is None:
            return None
        return self.get_registration(int(row["registration_id"]))

    def rewrite_registration_codes(self, code_generator: CodeGenerator) -> int:
        def callee(session):
            with session.transaction(tx_mode=ydb.QuerySerializableReadWrite()) as tx:
                rows = self._tx_execute(
                    tx,
                    """
                    SELECT id FROM registrations
                    ORDER BY id;
                    """,
                )
                used_codes: set[str] = set()
                next_codes: list[tuple[int, str]] = []
                for row in rows:
                    code = self._next_unique_rewrite_code(code_generator, used_codes)
                    used_codes.add(code)
                    next_codes.append((int(row["id"]), code))

                self._tx_execute(tx, "DELETE FROM registration_codes;")
                current = utc_now()
                for registration_id, code in next_codes:
                    self._tx_execute(
                        tx,
                        """
                        DECLARE $id AS Int64;
                        DECLARE $code AS Utf8;
                        DECLARE $updated_at AS Timestamp;
                        UPDATE registrations
                        SET code = $code, updated_at = $updated_at
                        WHERE id = $id;
                        """,
                        {
                            "$id": _int(registration_id),
                            "$code": _utf8(code),
                            "$updated_at": _timestamp(current),
                        },
                    )
                    self._tx_execute(
                        tx,
                        """
                        DECLARE $code AS Utf8;
                        DECLARE $registration_id AS Int64;
                        UPSERT INTO registration_codes (code, registration_id)
                        VALUES ($code, $registration_id);
                        """,
                        {
                            "$code": _utf8(code),
                            "$registration_id": _int(registration_id),
                        },
                    )
                self._tx_execute(tx, "SELECT 1;", commit=True)
                return len(next_codes)

        return self.pool.retry_operation_sync(
            callee,
            retry_settings=ydb.RetrySettings(max_retries=10, idempotent=False),
        )

    def close_registration(self, actor_user_id: int, event_id: int) -> Event:
        self._require_event_access(actor_user_id, event_id)
        self._execute(
            """
            DECLARE $id AS Int64;
            UPDATE events SET registration_closed = true WHERE id = $id;
            """,
            {"$id": _int(event_id)},
        )
        self._audit(actor_user_id, "event.registration_closed", "event", str(event_id), now=utc_now())
        return self._require_event(event_id)

    def mark_attended(
        self,
        actor_user_id: int,
        registration_id: int,
        *,
        now: datetime,
    ) -> Registration:
        registration = self._get_registration_for_actor(actor_user_id, registration_id)
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $status AS Utf8;
            DECLARE $attended_at AS Timestamp;
            DECLARE $updated_at AS Timestamp;
            UPDATE registrations
            SET status = $status, attended_at = $attended_at, updated_at = $updated_at
            WHERE id = $id;
            """,
            {
                "$id": _int(registration.id),
                "$status": _utf8(RegistrationStatus.ATTENDED.value),
                "$attended_at": _timestamp(now),
                "$updated_at": _timestamp(now),
            },
        )
        self._audit(actor_user_id, "registration.attended", "registration", str(registration_id), now=now)
        return self._require_registration(registration_id)

    def change_status(
        self,
        actor_user_id: int,
        registration_id: int,
        status: RegistrationStatus,
        *,
        now: datetime,
    ) -> Registration:
        current = _dt(now)

        def callee(session):
            with session.transaction(tx_mode=ydb.QuerySerializableReadWrite()) as tx:
                registration = self._tx_registration(tx, registration_id)
                if registration is None:
                    raise RegistrationNotFoundError("Запись не найдена")
                if not self._tx_has_event_access(tx, actor_user_id, registration.event_id):
                    raise AccessDeniedError("Нет доступа к этому мероприятию")
                was_active = registration.status in ACTIVE_REGISTRATION_STATUSES
                canceled_at = current if status not in ACTIVE_REGISTRATION_STATUSES else registration.canceled_at
                self._tx_update_registration_status(tx, registration, status, current, canceled_at=canceled_at)
                if was_active and status not in ACTIVE_REGISTRATION_STATUSES:
                    self._tx_remove_active_registration(tx, registration)
                    self._tx_decrease_booked_count(tx, registration)
                self._tx_audit(
                    tx,
                    actor_user_id,
                    "registration.status_changed",
                    "registration",
                    str(registration_id),
                    {"status": status.value},
                    now=current,
                )
                self._tx_execute(tx, "SELECT 1;", commit=True)
                return registration_id

        self.pool.retry_operation_sync(callee, retry_settings=ydb.RetrySettings(max_retries=10))
        return self._require_registration(registration_id)

    def enqueue_manual_notification(
        self,
        *,
        actor_user_id: int,
        event_id: int,
        kind: NotificationKind,
        message_text: str,
        now: datetime,
    ) -> list[NotificationOutbox]:
        if kind not in MANUAL_NOTIFICATION_KINDS:
            raise InvalidNotificationKindError("Этот тип уведомления нельзя отправить вручную")
        self._require_event_access(actor_user_id, event_id)
        rows = self._query(
            """
            DECLARE $event_id AS Int64;
            SELECT * FROM registrations VIEW idx_registrations_event
            WHERE event_id = $event_id AND notifications_enabled = true;
            """,
            {"$event_id": _int(event_id)},
        )
        created: list[NotificationOutbox] = []
        for registration in (_registration(row) for row in rows):
            if registration.status not in ACTIVE_REGISTRATION_STATUSES:
                continue
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
        if not item.id:
            item.id = self._new_id("notification_outbox")
        self._execute(_UPSERT_NOTIFICATION, _notification_params(item))
        return item

    def sync_registration_reminders(
        self,
        *,
        now: datetime,
        render_reminder: ReminderRenderer,
    ) -> int:
        current = _dt(now)
        changed = self._skip_legacy_reminders()
        registration_rows = self._query(
            """
            SELECT * FROM registrations
            WHERE notifications_enabled = true
              AND status IN ("confirmed", "attended");
            """
        )
        registrations = [_registration(row) for row in registration_rows]
        self._attach_registrations_batch(
            registrations,
            with_user=False,
            with_images=False,
        )
        notifications = self._notifications_by_registration_ids(
            [registration.id for registration in registrations]
        )
        for registration in registrations:
            if registration.event is None:
                continue
            changed += self._sync_registration_reminder_items(
                registration,
                registration.event,
                now=current,
                render_reminder=render_reminder,
                existing_items_by_kind=notifications.get(registration.id, {}),
            )
        return changed

    def list_notifications(self) -> list[NotificationOutbox]:
        rows = self._query("SELECT * FROM notification_outbox ORDER BY id;")
        return [self._attach_notification(_notification(row)) for row in rows]

    def list_due_notifications(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[NotificationOutbox]:
        rows = self._query(
            """
            DECLARE $now AS Timestamp;
            DECLARE $limit AS Uint64;
            SELECT * FROM notification_outbox VIEW idx_outbox_status_send
            WHERE status = "pending" AND send_after <= $now
            ORDER BY send_after, id
            LIMIT $limit;
            """,
            {"$now": _timestamp(now), "$limit": _uint64(limit)},
        )
        notifications = [_notification(row) for row in rows]
        self._attach_notifications_batch(notifications)
        return notifications

    def set_notification_result(
        self,
        notification_id: int,
        *,
        status: OutboxStatus,
        now: datetime,
        error: str | None = None,
    ) -> None:
        item = self._one(
            """
            DECLARE $id AS Int64;
            SELECT attempts, last_error, sent_at FROM notification_outbox WHERE id = $id;
            """,
            {"$id": _int(notification_id)},
        )
        if item is None:
            return
        attempts = int(item.get("attempts") or 0) + 1
        sent_at = _dt(now) if status == OutboxStatus.SENT else _row_dt(item, "sent_at")
        last_error = None if status == OutboxStatus.SENT else item.get("last_error")
        if status == OutboxStatus.FAILED:
            last_error = (error or "")[:1000]
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $status AS Utf8;
            DECLARE $attempts AS Int64;
            DECLARE $last_error AS Optional<Utf8>;
            DECLARE $sent_at AS Optional<Timestamp>;
            UPDATE notification_outbox
            SET status = $status,
                attempts = $attempts,
                last_error = $last_error,
                sent_at = $sent_at
            WHERE id = $id;
            """,
            {
                "$id": _int(notification_id),
                "$status": _utf8(status.value),
                "$attempts": _int(attempts),
                "$last_error": _opt_utf8(last_error),
                "$sent_at": _opt_timestamp(sent_at),
            },
        )

    def import_snapshot(self, snapshot) -> None:
        for user in snapshot.users:
            self._execute(
                """
                DECLARE $user_id AS Int64;
                DECLARE $display_name AS Utf8;
                DECLARE $is_bot AS Bool;
                DECLARE $created_at AS Timestamp;
                DECLARE $updated_at AS Timestamp;
                UPSERT INTO users (user_id, display_name, is_bot, created_at, updated_at)
                VALUES ($user_id, $display_name, $is_bot, $created_at, $updated_at);
                """,
                _user_params(user),
            )
        for event in snapshot.events:
            event.booked_count = 0
            self.add_event(
                event,
                slots=[
                    _reset_slot_counter(slot)
                    for slot in snapshot.slots
                    if slot.event_id == event.id
                ],
            )
        for consent in snapshot.consents:
            self._execute(
                """
                DECLARE $id AS Int64;
                DECLARE $user_id AS Int64;
                DECLARE $document_version AS Utf8;
                DECLARE $profile_data_allowed AS Bool;
                DECLARE $created_at AS Timestamp;
                UPSERT INTO consents (id, user_id, document_version, profile_data_allowed, created_at)
                VALUES ($id, $user_id, $document_version, $profile_data_allowed, $created_at);
                """,
                _consent_params(consent),
            )
        for role in snapshot.roles:
            self._execute(
                """
                DECLARE $id AS Int64;
                DECLARE $user_id AS Int64;
                DECLARE $role AS Utf8;
                DECLARE $created_at AS Optional<Timestamp>;
                DECLARE $created_by_user_id AS Optional<Int64>;
                UPSERT INTO role_assignments (
                    id, user_id, role, created_at, created_by_user_id
                )
                VALUES (
                    $id, $user_id, $role, $created_at, $created_by_user_id
                );
                """,
                _role_params(role),
            )
        for organizer_event in snapshot.organizer_events:
            self._execute(
                """
                DECLARE $id AS Int64;
                DECLARE $user_id AS Int64;
                DECLARE $event_id AS Int64;
                UPSERT INTO organizer_events (id, user_id, event_id)
                VALUES ($id, $user_id, $event_id);
                """,
                _organizer_event_params(organizer_event),
            )
        for registration in snapshot.registrations:
            self._execute(_UPSERT_REGISTRATION, _registration_params(registration))
            self._execute(
                """
                DECLARE $code AS Utf8;
                DECLARE $registration_id AS Int64;
                UPSERT INTO registration_codes (code, registration_id)
                VALUES ($code, $registration_id);
                """,
                {"$code": _utf8(registration.code), "$registration_id": _int(registration.id)},
            )
            if registration.status in ACTIVE_REGISTRATION_STATUSES:
                self._execute(
                    """
                    DECLARE $active_key AS Utf8;
                    DECLARE $registration_id AS Int64;
                    UPSERT INTO active_registration_keys (active_key, registration_id)
                    VALUES ($active_key, $registration_id);
                    """,
                    {
                        "$active_key": _utf8(self._active_key(registration.user_id, registration.event_id)),
                        "$registration_id": _int(registration.id),
                    },
                )
                if registration.slot_id is None:
                    self._execute(
                        """
                        DECLARE $id AS Int64;
                        UPDATE events SET booked_count = booked_count + 1 WHERE id = $id;
                        """,
                        {"$id": _int(registration.event_id)},
                    )
                else:
                    self._execute(
                        """
                        DECLARE $id AS Int64;
                        UPDATE event_slots SET booked_count = booked_count + 1 WHERE id = $id;
                        """,
                        {"$id": _int(registration.slot_id)},
                    )
        for notification in snapshot.notifications:
            self.add_notification(notification)
        for audit in snapshot.audit_logs:
            self._execute(_UPSERT_AUDIT, _audit_params(audit))

    def _require_event(self, event_id: int) -> Event:
        event = self.get_event(event_id)
        if event is None:
            raise EventNotFoundError("Мероприятие не найдено")
        return event

    def _require_slot(self, event: Event, slot_id: int) -> EventSlot:
        for slot in event.slots:
            if slot.id == slot_id:
                return slot
        raise SlotNotFoundError("Слот не найден")

    def _require_registration(self, registration_id: int) -> Registration:
        registration = self.get_registration(registration_id)
        if registration is None:
            raise RegistrationNotFoundError("Запись не найдена")
        return registration

    def _require_event_access(self, user_id: int, event_id: int) -> Event:
        if not self._has_event_access(user_id, event_id):
            raise AccessDeniedError("Нет доступа к этому мероприятию")
        event = self.get_event(event_id, with_slots=False, with_image=False)
        if event is None:
            raise EventNotFoundError("Мероприятие не найдено")
        return event

    def _require_event_creator(self, user_id: int) -> None:
        if self._is_admin(user_id) or self.has_role(user_id, "organizer"):
            return
        raise AccessDeniedError("Нет доступа к созданию мероприятий")

    def _active_event_registrations(self, event_id: int) -> list[Registration]:
        rows = self._query(
            """
            DECLARE $event_id AS Int64;
            DECLARE $confirmed AS Utf8;
            DECLARE $attended AS Utf8;
            SELECT * FROM registrations VIEW idx_registrations_event
            WHERE event_id = $event_id AND (status = $confirmed OR status = $attended);
            """,
            {
                "$event_id": _int(event_id),
                "$confirmed": _utf8(RegistrationStatus.CONFIRMED.value),
                "$attended": _utf8(RegistrationStatus.ATTENDED.value),
            },
        )
        return [_registration(row) for row in rows]

    def _set_event_image_without_access_check(
        self,
        actor_user_id: int,
        event_id: int,
        *,
        token: str | None,
        url: str | None,
        now: datetime,
    ) -> None:
        clean_token = (token or "").strip() or None
        clean_url = (url or "").strip() or None
        self._execute(
            """
            DECLARE $event_id AS Int64;
            DECLARE $token AS Optional<Utf8>;
            DECLARE $url AS Optional<Utf8>;
            DECLARE $updated_by_user_id AS Int64;
            DECLARE $updated_at AS Timestamp;
            UPSERT INTO event_images (
                event_id, token, url, updated_by_user_id, updated_at
            ) VALUES (
                $event_id, $token, $url, $updated_by_user_id, $updated_at
            );
            """,
            {
                "$event_id": _int(event_id),
                "$token": _opt_utf8(clean_token),
                "$url": _opt_utf8(clean_url),
                "$updated_by_user_id": _int(actor_user_id),
                "$updated_at": _timestamp(now),
            },
        )
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

    def _is_admin(self, user_id: int) -> bool:
        row = self._one(
            """
            DECLARE $user_id AS Int64;
            SELECT id FROM role_assignments VIEW idx_roles_user
            WHERE user_id = $user_id AND role = "admin"
            LIMIT 1;
            """,
            {"$user_id": _int(user_id)},
        )
        return row is not None

    def _has_event_access(self, user_id: int, event_id: int) -> bool:
        if self._is_admin(user_id):
            return True
        row = self._one(
            """
            DECLARE $user_id AS Int64;
            DECLARE $event_id AS Int64;
            SELECT id FROM organizer_events VIEW idx_organizer_events_user
            WHERE user_id = $user_id AND event_id = $event_id
            LIMIT 1;
            """,
            {"$user_id": _int(user_id), "$event_id": _int(event_id)},
        )
        return row is not None

    def _event_slots(self, event_id: int) -> list[EventSlot]:
        rows = self._query(
            """
            DECLARE $event_id AS Int64;
            SELECT * FROM event_slots VIEW idx_slots_event
            WHERE event_id = $event_id
            ORDER BY starts_at;
            """,
            {"$event_id": _int(event_id)},
        )
        return [_slot(row) for row in rows]

    def _event_slots_for_events(self, event_ids: list[int] | set[int]) -> dict[int, list[EventSlot]]:
        ids = sorted({int(event_id) for event_id in event_ids})
        if not ids:
            return {}
        rows = self._query(
            """
            DECLARE $event_ids AS List<Int64>;
            SELECT * FROM event_slots VIEW idx_slots_event
            WHERE event_id IN $event_ids
            ORDER BY event_id, starts_at;
            """,
            {"$event_ids": _int_list(ids)},
        )
        slots_by_event: dict[int, list[EventSlot]] = {event_id: [] for event_id in ids}
        for row in rows:
            slot = _slot(row)
            slots_by_event.setdefault(slot.event_id, []).append(slot)
        return slots_by_event

    def _attach_event_images_for_events(self, events: list[Event]) -> None:
        if not events:
            return
        event_ids = [event.id for event in events]
        rows = self._query(
            """
            DECLARE $event_ids AS List<Int64>;
            SELECT event_id, token, url FROM event_images
            WHERE event_id IN $event_ids;
            """,
            {"$event_ids": _int_list(event_ids)},
        )
        images = {int(row["event_id"]): row for row in rows}
        for event in events:
            row = images.get(event.id)
            if row is None:
                event.image_token = None
                event.image_url = None
                continue
            token = row.get("token")
            url = row.get("url")
            event.image_token = str(token) if token else None
            event.image_url = str(url) if url else None

    def _events_by_ids(
        self,
        event_ids: list[int] | set[int],
        *,
        with_slots: bool = True,
        with_images: bool = True,
    ) -> dict[int, Event]:
        ids = sorted({int(event_id) for event_id in event_ids})
        if not ids:
            return {}
        rows = self._query(
            """
            DECLARE $event_ids AS List<Int64>;
            SELECT * FROM events
            WHERE id IN $event_ids;
            """,
            {"$event_ids": _int_list(ids)},
        )
        events = {int(row["id"]): _event(row) for row in rows}
        if with_slots:
            slots_by_event = self._event_slots_for_events(list(events.keys()))
            for event in events.values():
                event.slots = slots_by_event.get(event.id, [])
        if with_images:
            self._attach_event_images_for_events(list(events.values()))
        return events

    def _attach_registrations_batch(
        self,
        registrations: list[Registration],
        *,
        with_event: bool = True,
        with_event_slots: bool = True,
        with_slot: bool = True,
        with_user: bool = True,
        with_images: bool = True,
    ) -> None:
        if not registrations:
            return
        events: dict[int, Event] = {}
        if with_event:
            events = self._events_by_ids(
                {registration.event_id for registration in registrations},
                with_slots=with_event_slots,
                with_images=with_images,
            )
        slots: dict[int, EventSlot] = {}
        if with_slot:
            slot_ids = sorted(
                {
                    int(registration.slot_id)
                    for registration in registrations
                    if registration.slot_id is not None
                }
            )
            if slot_ids:
                rows = self._query(
                    """
                    DECLARE $slot_ids AS List<Int64>;
                    SELECT * FROM event_slots
                    WHERE id IN $slot_ids;
                    """,
                    {"$slot_ids": _int_list(slot_ids)},
                )
                slots = {int(row["id"]): _slot(row) for row in rows}
        users: dict[int, User] = {}
        if with_user:
            user_ids = sorted({registration.user_id for registration in registrations})
            rows = self._query(
                """
                DECLARE $user_ids AS List<Int64>;
                SELECT * FROM users
                WHERE user_id IN $user_ids;
                """,
                {"$user_ids": _int_list(user_ids)},
            )
            users = {int(row["user_id"]): _user(row) for row in rows}
        for registration in registrations:
            registration.event = events.get(registration.event_id) if with_event else None
            registration.slot = slots.get(registration.slot_id) if registration.slot_id else None
            registration.user = users.get(registration.user_id) if with_user else None

    @staticmethod
    def _registration_without_related_objects(registration: Registration) -> Registration:
        return Registration(
            id=registration.id,
            code=registration.code,
            user_id=registration.user_id,
            event_id=registration.event_id,
            slot_id=registration.slot_id,
            status=registration.status,
            notifications_enabled=registration.notifications_enabled,
            created_at=registration.created_at,
            updated_at=registration.updated_at,
            canceled_at=registration.canceled_at,
            attended_at=registration.attended_at,
        )

    def _attach_registration(self, registration: Registration) -> Registration:
        registration.event = self.get_event(registration.event_id)
        registration.slot = self._get_slot(registration.slot_id) if registration.slot_id else None
        registration.user = self.get_user(registration.user_id)
        return registration

    def _attach_event_image(self, event: Event) -> Event:
        row = self._one(
            """
            DECLARE $event_id AS Int64;
            SELECT token, url FROM event_images WHERE event_id = $event_id;
            """,
            {"$event_id": _int(event.id)},
        )
        if row is None:
            event.image_token = None
            event.image_url = None
            return event
        token = row.get("token")
        url = row.get("url")
        event.image_token = str(token) if token else None
        event.image_url = str(url) if url else None
        return event

    def _attach_notifications_batch(self, notifications: list[NotificationOutbox]) -> None:
        registration_ids = sorted(
            {
                int(item.registration_id)
                for item in notifications
                if item.registration_id is not None
            }
        )
        if not registration_ids:
            return
        rows = self._query(
            """
            DECLARE $registration_ids AS List<Int64>;
            SELECT * FROM registrations
            WHERE id IN $registration_ids;
            """,
            {"$registration_ids": _int_list(registration_ids)},
        )
        registrations = [_registration(row) for row in rows]
        self._attach_registrations_batch(registrations, with_user=False, with_images=False)
        registrations_by_id = {registration.id: registration for registration in registrations}
        for item in notifications:
            if item.registration_id is not None:
                item.registration = registrations_by_id.get(item.registration_id)

    def _delete_registration_cascade(self, registration: Registration) -> None:
        self._execute(
            """
            DECLARE $registration_id AS Int64;
            DELETE FROM notification_outbox WHERE registration_id = $registration_id;
            """,
            {"$registration_id": _int(registration.id)},
        )
        self._execute(
            """
            DECLARE $code AS Utf8;
            DELETE FROM registration_codes WHERE code = $code;
            """,
            {"$code": _utf8(registration.code)},
        )
        self._execute(
            """
            DECLARE $active_key AS Utf8;
            DELETE FROM active_registration_keys WHERE active_key = $active_key;
            """,
            {"$active_key": _utf8(self._active_key(registration.user_id, registration.event_id))},
        )
        self._execute(
            """
            DECLARE $entity_type AS Utf8;
            DECLARE $entity_id AS Utf8;
            DELETE FROM audit_log
            WHERE entity_type = $entity_type AND entity_id = $entity_id;
            """,
            {
                "$entity_type": _utf8("registration"),
                "$entity_id": _utf8(str(registration.id)),
            },
        )
        self._execute(
            """
            DECLARE $id AS Int64;
            DELETE FROM registrations WHERE id = $id;
            """,
            {"$id": _int(registration.id)},
        )

    def _delete_event_cascade(self, event_id: int) -> None:
        registration_rows = self._query(
            """
            DECLARE $event_id AS Int64;
            SELECT id, code, user_id FROM registrations
            WHERE event_id = $event_id;
            """,
            {"$event_id": _int(event_id)},
        )
        for row in registration_rows:
            registration_id = int(row["id"])
            code = str(row["code"])
            user_id = int(row["user_id"])
            self._execute(
                """
                DECLARE $registration_id AS Int64;
                DELETE FROM notification_outbox WHERE registration_id = $registration_id;
                """,
                {"$registration_id": _int(registration_id)},
            )
            self._execute(
                """
                DECLARE $code AS Utf8;
                DELETE FROM registration_codes WHERE code = $code;
                """,
                {"$code": _utf8(code)},
            )
            self._execute(
                """
                DECLARE $active_key AS Utf8;
                DELETE FROM active_registration_keys WHERE active_key = $active_key;
                """,
                {"$active_key": _utf8(self._active_key(user_id, event_id))},
            )
            self._execute(
                """
                DECLARE $entity_type AS Utf8;
                DECLARE $entity_id AS Utf8;
                DELETE FROM audit_log
                WHERE entity_type = $entity_type AND entity_id = $entity_id;
                """,
                {
                    "$entity_type": _utf8("registration"),
                    "$entity_id": _utf8(str(registration_id)),
                },
            )

        delete_queries = [
            "DELETE FROM notification_outbox WHERE event_id = $event_id;",
            "DELETE FROM registrations WHERE event_id = $event_id;",
            "DELETE FROM event_slots WHERE event_id = $event_id;",
            "DELETE FROM organizer_events WHERE event_id = $event_id;",
            "DELETE FROM event_deeplinks WHERE event_id = $event_id;",
            "DELETE FROM event_images WHERE event_id = $event_id;",
            "DELETE FROM pending_event_images WHERE event_id = $event_id;",
            "DELETE FROM organizer_states WHERE event_id = $event_id;",
        ]
        for query in delete_queries:
            self._execute(
                f"""
                DECLARE $event_id AS Int64;
                {query}
                """,
                {"$event_id": _int(event_id)},
            )
        self._execute(
            """
            DECLARE $entity_type AS Utf8;
            DECLARE $entity_id AS Utf8;
            DELETE FROM audit_log
            WHERE entity_type = $entity_type AND entity_id = $entity_id;
            """,
            {"$entity_type": _utf8("event"), "$entity_id": _utf8(str(event_id))},
        )
        self._execute(
            """
            DECLARE $id AS Int64;
            DELETE FROM events WHERE id = $id;
            """,
            {"$id": _int(event_id)},
        )

    def _attach_notification(self, item: NotificationOutbox) -> NotificationOutbox:
        if item.registration_id is not None:
            item.registration = self.get_registration(item.registration_id)
        return item

    def _get_slot(self, slot_id: int | None) -> EventSlot | None:
        if slot_id is None:
            return None
        row = self._one(
            """
            DECLARE $id AS Int64;
            SELECT * FROM event_slots WHERE id = $id;
            """,
            {"$id": _int(slot_id)},
        )
        return _slot(row) if row else None

    def _new_id(self, table_name: str) -> int:
        for _ in range(20):
            value = secrets.randbits(62)
            row = self._one(
                f"""
                DECLARE $id AS Int64;
                SELECT id FROM {table_name} WHERE id = $id;
                """,
                {"$id": _int(value)},
            )
            if row is None:
                return value
        raise RuntimeError("Не удалось сгенерировать идентификатор")

    def _query(self, query: str, params: dict | None = None):
        started_at = time.perf_counter()
        try:
            result_sets = self.pool.execute_with_retries(
                query,
                params,
                retry_settings=ydb.RetrySettings(idempotent=True),
            )
            return _rows(result_sets)
        finally:
            record_method("ydb", "query", (time.perf_counter() - started_at) * 1000)

    def _one(self, query: str, params: dict | None = None):
        rows = self._query(query, params)
        return rows[0] if rows else None

    def _execute(self, query: str, params: dict | None = None) -> None:
        started_at = time.perf_counter()
        try:
            self.pool.execute_with_retries(
                query,
                params,
                retry_settings=ydb.RetrySettings(max_retries=10, idempotent=True),
            )
        finally:
            record_method("ydb", "execute", (time.perf_counter() - started_at) * 1000)

    def _tx_execute(self, tx, query: str, params: dict | None = None, *, commit: bool = False):
        started_at = time.perf_counter()
        try:
            with tx.execute(query, params, commit_tx=commit) as result_sets:
                return _rows(result_sets)
        finally:
            record_method("ydb", "tx_execute", (time.perf_counter() - started_at) * 1000)

    def _tx_one(self, tx, query: str, params: dict | None = None):
        rows = self._tx_execute(tx, query, params)
        return rows[0] if rows else None

    def _tx_has_consent(self, tx, user_id: int) -> bool:
        return self._tx_one(
            tx,
            """
            DECLARE $user_id AS Int64;
            SELECT id FROM consents VIEW idx_consents_user
            WHERE user_id = $user_id AND profile_data_allowed = true
            LIMIT 1;
            """,
            {"$user_id": _int(user_id)},
        ) is not None

    def _tx_event(self, tx, event_id: int) -> Event | None:
        row = self._tx_one(
            tx,
            """
            DECLARE $id AS Int64;
            SELECT * FROM events WHERE id = $id;
            """,
            {"$id": _int(event_id)},
        )
        return _event(row) if row else None

    def _tx_event_slots(self, tx, event_id: int) -> list[EventSlot]:
        rows = self._tx_execute(
            tx,
            """
            DECLARE $event_id AS Int64;
            SELECT * FROM event_slots VIEW idx_slots_event
            WHERE event_id = $event_id
            ORDER BY starts_at;
            """,
            {"$event_id": _int(event_id)},
        )
        return [_slot(row) for row in rows]

    def _tx_registration(self, tx, registration_id: int) -> Registration | None:
        row = self._tx_one(
            tx,
            """
            DECLARE $id AS Int64;
            SELECT * FROM registrations WHERE id = $id;
            """,
            {"$id": _int(registration_id)},
        )
        return _registration(row) if row else None

    def _tx_has_event_access(self, tx, user_id: int, event_id: int) -> bool:
        if self._tx_one(
            tx,
            """
            DECLARE $user_id AS Int64;
            SELECT id FROM role_assignments VIEW idx_roles_user
            WHERE user_id = $user_id AND role = "admin"
            LIMIT 1;
            """,
            {"$user_id": _int(user_id)},
        ):
            return True
        return self._tx_one(
            tx,
            """
            DECLARE $user_id AS Int64;
            DECLARE $event_id AS Int64;
            SELECT id FROM organizer_events VIEW idx_organizer_events_user
            WHERE user_id = $user_id AND event_id = $event_id
            LIMIT 1;
            """,
            {"$user_id": _int(user_id), "$event_id": _int(event_id)},
        ) is not None

    def _tx_active_key_exists(self, tx, active_key: str) -> bool:
        return self._tx_one(
            tx,
            """
            DECLARE $active_key AS Utf8;
            SELECT registration_id FROM active_registration_keys WHERE active_key = $active_key;
            """,
            {"$active_key": _utf8(active_key)},
        ) is not None

    def _tx_next_unique_code(self, tx, code_generator: CodeGenerator) -> str:
        for _ in range(20):
            code = code_generator().strip().upper()
            if not code:
                continue
            exists = self._tx_one(
                tx,
                """
                DECLARE $code AS Utf8;
                SELECT registration_id FROM registration_codes WHERE code = $code;
                """,
                {"$code": _utf8(code)},
            )
            if exists is None:
                return code
        raise RuntimeError("Не удалось сгенерировать уникальный код записи")

    @staticmethod
    def _next_unique_rewrite_code(
        code_generator: CodeGenerator,
        used_codes: set[str],
    ) -> str:
        for _ in range(20):
            code = code_generator().strip().upper()
            if code and code not in used_codes:
                return code
        raise RuntimeError("Не удалось сгенерировать уникальный код записи")

    def _tx_update_registration_status(
        self,
        tx,
        registration: Registration,
        status: RegistrationStatus,
        updated_at: datetime,
        *,
        canceled_at: datetime | None,
    ) -> None:
        self._tx_execute(
            tx,
            """
            DECLARE $id AS Int64;
            DECLARE $status AS Utf8;
            DECLARE $updated_at AS Timestamp;
            DECLARE $canceled_at AS Optional<Timestamp>;
            UPDATE registrations
            SET status = $status, updated_at = $updated_at, canceled_at = $canceled_at
            WHERE id = $id;
            """,
            {
                "$id": _int(registration.id),
                "$status": _utf8(status.value),
                "$updated_at": _timestamp(updated_at),
                "$canceled_at": _opt_timestamp(canceled_at),
            },
        )

    def _tx_remove_active_registration(self, tx, registration: Registration) -> None:
        self._tx_execute(
            tx,
            """
            DECLARE $active_key AS Utf8;
            DELETE FROM active_registration_keys WHERE active_key = $active_key;
            """,
            {"$active_key": _utf8(self._active_key(registration.user_id, registration.event_id))},
        )

    def _tx_decrease_booked_count(self, tx, registration: Registration) -> None:
        if registration.slot_id is None:
            self._tx_execute(
                tx,
                """
                DECLARE $id AS Int64;
                UPDATE events
                SET booked_count = CASE WHEN booked_count > 0 THEN booked_count - 1 ELSE 0 END
                WHERE id = $id;
                """,
                {"$id": _int(registration.event_id)},
            )
        else:
            self._tx_execute(
                tx,
                """
                DECLARE $id AS Int64;
                UPDATE event_slots
                SET booked_count = CASE WHEN booked_count > 0 THEN booked_count - 1 ELSE 0 END
                WHERE id = $id;
                """,
                {"$id": _int(registration.slot_id)},
            )

    def _tx_schedule_reminders(
        self,
        tx,
        registration: Registration,
        event: Event,
        now: datetime,
        render_reminder: ReminderRenderer,
    ) -> None:
        for kind, send_after in automatic_reminder_schedule(event, registration):
            if send_after < now:
                continue
            item = NotificationOutbox(
                id=self._new_id("notification_outbox"),
                event_id=event.id,
                registration_id=registration.id,
                user_id=registration.user_id,
                kind=kind,
                message_text=render_reminder(kind, event, registration),
                send_after=send_after,
                created_at=now,
            )
            self._tx_execute(tx, _UPSERT_NOTIFICATION, _notification_params(item))

    def _skip_legacy_reminders(self) -> int:
        changed = 0
        for kind in LEGACY_AUTOMATIC_REMINDER_KINDS:
            rows = self._query(
                """
                DECLARE $kind AS Utf8;
                SELECT id FROM notification_outbox
                WHERE kind = $kind AND status = "pending";
                """,
                {"$kind": _utf8(kind.value)},
            )
            changed += len(rows)
            self._execute(
                """
                DECLARE $kind AS Utf8;
                DECLARE $status AS Utf8;
                DECLARE $last_error AS Optional<Utf8>;
                UPDATE notification_outbox
                SET status = $status,
                    last_error = $last_error
                WHERE kind = $kind AND status = "pending";
                """,
                {
                    "$kind": _utf8(kind.value),
                    "$status": _utf8(OutboxStatus.SKIPPED.value),
                    "$last_error": _opt_utf8("Заменено новой схемой напоминаний"),
                },
            )
        return changed

    def _sync_registration_reminder_items(
        self,
        registration: Registration,
        event: Event,
        *,
        now: datetime,
        render_reminder: ReminderRenderer,
        existing_items_by_kind: dict[NotificationKind, list[NotificationOutbox]] | None = None,
    ) -> int:
        changed = 0
        for kind, send_after in automatic_reminder_schedule(event, registration):
            if existing_items_by_kind is None:
                rows = self._query(
                    """
                    DECLARE $registration_id AS Int64;
                    DECLARE $kind AS Utf8;
                    SELECT * FROM notification_outbox VIEW idx_outbox_registration
                    WHERE registration_id = $registration_id AND kind = $kind
                    ORDER BY id;
                    """,
                    {
                        "$registration_id": _int(registration.id),
                        "$kind": _utf8(kind.value),
                    },
                )
                items = [_notification(row) for row in rows]
            else:
                items = list(existing_items_by_kind.get(kind, []))
            pending_items = [item for item in items if item.status == OutboxStatus.PENDING]
            has_non_skipped = any(item.status != OutboxStatus.SKIPPED for item in items)
            if send_after < now and not pending_items:
                continue
            if send_after < now and kind != NotificationKind.REMINDER_START:
                for item in pending_items:
                    self._update_notification_status(
                        item.id,
                        status=OutboxStatus.SKIPPED,
                        error="Срок напоминания уже прошел",
                    )
                    changed += 1
                continue
            message_text = render_reminder(kind, event, registration)
            if pending_items:
                item = pending_items[0]
                if (
                    item.send_after != send_after
                    or item.message_text != message_text
                    or item.event_id != event.id
                    or item.user_id != registration.user_id
                ):
                    self._update_pending_reminder(
                        item.id,
                        event_id=event.id,
                        user_id=registration.user_id,
                        message_text=message_text,
                        send_after=send_after,
                    )
                    changed += 1
                for duplicate in pending_items[1:]:
                    self._update_notification_status(
                        duplicate.id,
                        status=OutboxStatus.SKIPPED,
                        error="Дубликат автоматического напоминания",
                    )
                    changed += 1
                continue
            if has_non_skipped:
                continue
            self.add_notification(
                NotificationOutbox(
                    id=0,
                    event_id=event.id,
                    registration_id=registration.id,
                    user_id=registration.user_id,
                    kind=kind,
                    message_text=message_text,
                    send_after=send_after,
                    created_at=now,
                )
            )
            changed += 1
        return changed

    def _notifications_by_registration_ids(
        self,
        registration_ids: list[int],
    ) -> dict[int, dict[NotificationKind, list[NotificationOutbox]]]:
        ids = sorted({int(registration_id) for registration_id in registration_ids})
        if not ids:
            return {}
        rows = self._query(
            """
            DECLARE $registration_ids AS List<Int64>;
            SELECT * FROM notification_outbox VIEW idx_outbox_registration
            WHERE registration_id IN $registration_ids
            ORDER BY registration_id, kind, id;
            """,
            {"$registration_ids": _int_list(ids)},
        )
        grouped: dict[int, dict[NotificationKind, list[NotificationOutbox]]] = {
            registration_id: {} for registration_id in ids
        }
        for row in rows:
            item = _notification(row)
            grouped.setdefault(item.registration_id or 0, {}).setdefault(item.kind, []).append(item)
        return grouped

    def _update_pending_reminder(
        self,
        notification_id: int,
        *,
        event_id: int,
        user_id: int,
        message_text: str,
        send_after: datetime,
    ) -> None:
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $event_id AS Int64;
            DECLARE $user_id AS Int64;
            DECLARE $message_text AS Utf8;
            DECLARE $send_after AS Timestamp;
            UPDATE notification_outbox
            SET event_id = $event_id,
                user_id = $user_id,
                message_text = $message_text,
                send_after = $send_after
            WHERE id = $id;
            """,
            {
                "$id": _int(notification_id),
                "$event_id": _int(event_id),
                "$user_id": _int(user_id),
                "$message_text": _utf8(message_text),
                "$send_after": _timestamp(send_after),
                },
            )

    def _tx_delete_registration_cascade(self, tx, registration: Registration) -> None:
        self._tx_execute(
            tx,
            """
            DECLARE $registration_id AS Int64;
            DELETE FROM notification_outbox WHERE registration_id = $registration_id;
            """,
            {"$registration_id": _int(registration.id)},
        )
        self._tx_execute(
            tx,
            """
            DECLARE $code AS Utf8;
            DELETE FROM registration_codes WHERE code = $code;
            """,
            {"$code": _utf8(registration.code)},
        )
        self._tx_execute(
            tx,
            """
            DECLARE $active_key AS Utf8;
            DELETE FROM active_registration_keys WHERE active_key = $active_key;
            """,
            {"$active_key": _utf8(self._active_key(registration.user_id, registration.event_id))},
        )
        self._tx_execute(
            tx,
            """
            DECLARE $entity_type AS Utf8;
            DECLARE $entity_id AS Utf8;
            DELETE FROM audit_log
            WHERE entity_type = $entity_type AND entity_id = $entity_id;
            """,
            {
                "$entity_type": _utf8("registration"),
                "$entity_id": _utf8(str(registration.id)),
            },
        )
        self._tx_execute(
            tx,
            """
            DECLARE $id AS Int64;
            DELETE FROM registrations WHERE id = $id;
            """,
            {"$id": _int(registration.id)},
        )

    def _update_notification_status(
        self,
        notification_id: int,
        *,
        status: OutboxStatus,
        error: str,
    ) -> None:
        self._execute(
            """
            DECLARE $id AS Int64;
            DECLARE $status AS Utf8;
            DECLARE $last_error AS Optional<Utf8>;
            UPDATE notification_outbox
            SET status = $status,
                last_error = $last_error
            WHERE id = $id;
            """,
            {
                "$id": _int(notification_id),
                "$status": _utf8(status.value),
                "$last_error": _opt_utf8(error),
            },
        )

    def _tx_audit(
        self,
        tx,
        actor_user_id: int | None,
        action: str,
        entity_type: str,
        entity_id: str,
        metadata: dict | None = None,
        *,
        now: datetime,
    ) -> None:
        item = AuditLog(
            id=self._new_id("audit_log"),
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_json=metadata or {},
            created_at=now,
        )
        self._tx_execute(tx, _UPSERT_AUDIT, _audit_params(item))

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
        item = AuditLog(
            id=self._new_id("audit_log"),
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_json=metadata or {},
            created_at=_dt(now or utc_now()),
        )
        self._execute(_UPSERT_AUDIT, _audit_params(item))

    @staticmethod
    def _tx_available_places(event: Event, slots: list[EventSlot], slot_id: int | None) -> int:
        if slots:
            if slot_id is None:
                return sum(max(slot.capacity - slot.booked_count, 0) for slot in slots)
            slot = next(slot for slot in slots if slot.id == slot_id)
            return max(slot.capacity - slot.booked_count, 0)
        return max(event.capacity_total - event.booked_count, 0)

    @staticmethod
    def _active_key(user_id: int, event_id: int) -> str:
        return f"{user_id}:{event_id}"

    @staticmethod
    def _credentials(use_metadata_credentials: bool):
        if os.getenv("YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS") or os.getenv("YDB_ACCESS_TOKEN_CREDENTIALS"):
            return ydb.credentials_from_env_variables()
        if use_metadata_credentials:
            os.environ["YDB_METADATA_CREDENTIALS"] = "1"
            return ydb.credentials_from_env_variables()
        return ydb.AnonymousCredentials()


_UPSERT_EVENT = """
DECLARE $id AS Int64;
DECLARE $title AS Utf8;
DECLARE $description AS Utf8;
DECLARE $requirements AS Utf8;
DECLARE $starts_at AS Timestamp;
DECLARE $duration_minutes AS Int64;
DECLARE $format AS Utf8;
DECLARE $location_or_url AS Utf8;
DECLARE $cancellation_policy_text AS Utf8;
DECLARE $capacity_total AS Int64;
DECLARE $registration_closed AS Bool;
DECLARE $late_cancel_policy AS Utf8;
DECLARE $created_at AS Timestamp;
DECLARE $booked_count AS Int64;
UPSERT INTO events (
    id, title, description, requirements, starts_at, duration_minutes, format,
    location_or_url, cancellation_policy_text, capacity_total, registration_closed,
    late_cancel_policy, created_at, booked_count
) VALUES (
    $id, $title, $description, $requirements, $starts_at, $duration_minutes, $format,
    $location_or_url, $cancellation_policy_text, $capacity_total, $registration_closed,
    $late_cancel_policy, $created_at, $booked_count
);
"""

_UPSERT_SLOT = """
DECLARE $id AS Int64;
DECLARE $event_id AS Int64;
DECLARE $title AS Utf8;
DECLARE $starts_at AS Timestamp;
DECLARE $ends_at AS Timestamp;
DECLARE $capacity AS Int64;
DECLARE $booked_count AS Int64;
UPSERT INTO event_slots (id, event_id, title, starts_at, ends_at, capacity, booked_count)
VALUES ($id, $event_id, $title, $starts_at, $ends_at, $capacity, $booked_count);
"""

_UPSERT_REGISTRATION = """
DECLARE $id AS Int64;
DECLARE $code AS Utf8;
DECLARE $user_id AS Int64;
DECLARE $event_id AS Int64;
DECLARE $slot_id AS Optional<Int64>;
DECLARE $status AS Utf8;
DECLARE $notifications_enabled AS Bool;
DECLARE $created_at AS Timestamp;
DECLARE $updated_at AS Timestamp;
DECLARE $canceled_at AS Optional<Timestamp>;
DECLARE $attended_at AS Optional<Timestamp>;
UPSERT INTO registrations (
    id, code, user_id, event_id, slot_id, status, notifications_enabled,
    created_at, updated_at, canceled_at, attended_at
) VALUES (
    $id, $code, $user_id, $event_id, $slot_id, $status, $notifications_enabled,
    $created_at, $updated_at, $canceled_at, $attended_at
);
"""

_UPSERT_NOTIFICATION = """
DECLARE $id AS Int64;
DECLARE $event_id AS Int64;
DECLARE $registration_id AS Optional<Int64>;
DECLARE $user_id AS Int64;
DECLARE $kind AS Utf8;
DECLARE $message_text AS Utf8;
DECLARE $send_after AS Timestamp;
DECLARE $status AS Utf8;
DECLARE $attempts AS Int64;
DECLARE $last_error AS Optional<Utf8>;
DECLARE $created_at AS Timestamp;
DECLARE $sent_at AS Optional<Timestamp>;
UPSERT INTO notification_outbox (
    id, event_id, registration_id, user_id, kind, message_text, send_after,
    status, attempts, last_error, created_at, sent_at
) VALUES (
    $id, $event_id, $registration_id, $user_id, $kind, $message_text, $send_after,
    $status, $attempts, $last_error, $created_at, $sent_at
);
"""

_UPSERT_AUDIT = """
DECLARE $id AS Int64;
DECLARE $actor_user_id AS Optional<Int64>;
DECLARE $action AS Utf8;
DECLARE $entity_type AS Utf8;
DECLARE $entity_id AS Utf8;
DECLARE $metadata_json AS Utf8;
DECLARE $created_at AS Timestamp;
UPSERT INTO audit_log (id, actor_user_id, action, entity_type, entity_id, metadata_json, created_at)
VALUES ($id, $actor_user_id, $action, $entity_type, $entity_id, $metadata_json, $created_at);
"""


def _rows(result_sets) -> list:
    if not result_sets:
        return []
    if isinstance(result_sets, (list, tuple)):
        first = result_sets[0]
    else:
        first = next(iter(result_sets), None)
        if first is None:
            return []
    return list(getattr(first, "rows", []) or [])


def _dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _row_dt(row, name: str) -> datetime | None:
    value = row.get(name)
    if value is None:
        return None
    if isinstance(value, datetime):
        return _dt(value)
    return value


def _int(value: int) -> ydb.TypedValue:
    return ydb.TypedValue(int(value), ydb.PrimitiveType.Int64)


def _int_list(values) -> ydb.TypedValue:
    return ydb.TypedValue(
        [int(value) for value in values],
        ydb.ListType(ydb.PrimitiveType.Int64),
    )


def _uint64(value: int) -> ydb.TypedValue:
    return ydb.TypedValue(int(value), ydb.PrimitiveType.Uint64)


def _opt_int(value: int | None) -> ydb.TypedValue:
    return ydb.TypedValue(None if value is None else int(value), ydb.OptionalType(ydb.PrimitiveType.Int64))


def _utf8(value: str) -> ydb.TypedValue:
    return ydb.TypedValue(str(value or ""), ydb.PrimitiveType.Utf8)


def _opt_utf8(value: str | None) -> ydb.TypedValue:
    return ydb.TypedValue(value, ydb.OptionalType(ydb.PrimitiveType.Utf8))


def _bool(value: bool) -> ydb.TypedValue:
    return ydb.TypedValue(bool(value), ydb.PrimitiveType.Bool)


def _timestamp(value: datetime) -> ydb.TypedValue:
    return ydb.TypedValue(_dt(value), ydb.PrimitiveType.Timestamp)


def _opt_timestamp(value: datetime | None) -> ydb.TypedValue:
    return ydb.TypedValue(_dt(value) if value else None, ydb.OptionalType(ydb.PrimitiveType.Timestamp))


def _user(row) -> User:
    return User(
        user_id=int(row["user_id"]),
        display_name=row.get("display_name") or "",
        is_bot=bool(row.get("is_bot") or False),
        created_at=_row_dt(row, "created_at") or utc_now(),
        updated_at=_row_dt(row, "updated_at") or utc_now(),
    )


def _role(row) -> RoleAssignment:
    return RoleAssignment(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        role=row.get("role") or "",
        created_at=_row_dt(row, "created_at"),
        created_by_user_id=(
            int(row["created_by_user_id"])
            if row.get("created_by_user_id") is not None
            else None
        ),
    )


def _event(row) -> Event:
    return Event(
        id=int(row["id"]),
        title=row.get("title") or "",
        description=row.get("description") or "",
        requirements=row.get("requirements") or "",
        starts_at=_row_dt(row, "starts_at") or utc_now(),
        duration_minutes=int(row.get("duration_minutes") or 0),
        format=EventFormat(row.get("format") or EventFormat.IN_PERSON.value),
        location_or_url=row.get("location_or_url") or "",
        cancellation_policy_text=row.get("cancellation_policy_text") or "",
        capacity_total=int(row.get("capacity_total") or 0),
        registration_closed=bool(row.get("registration_closed") or False),
        late_cancel_policy=LateCancelPolicy(row.get("late_cancel_policy") or LateCancelPolicy.DENY.value),
        created_at=_row_dt(row, "created_at") or utc_now(),
        booked_count=int(row.get("booked_count") or 0),
    )


def _slot(row) -> EventSlot:
    return EventSlot(
        id=int(row["id"]),
        event_id=int(row.get("event_id") or 0),
        title=row.get("title") or "",
        starts_at=_row_dt(row, "starts_at") or utc_now(),
        ends_at=_row_dt(row, "ends_at") or utc_now(),
        capacity=int(row.get("capacity") or 0),
        booked_count=int(row.get("booked_count") or 0),
    )


def _organizer_state(row) -> OrganizerState:
    raw_data = row.get("data_json") or "{}"
    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError:
        data = {}
    return OrganizerState(
        user_id=int(row["user_id"]),
        mode=row.get("mode") or "",
        event_id=row.get("event_id"),
        step=row.get("step") or "",
        data=data if isinstance(data, dict) else {},
        updated_at=_row_dt(row, "updated_at") or utc_now(),
    )


def _registration(row) -> Registration:
    return Registration(
        id=int(row["id"]),
        code=row.get("code") or "",
        user_id=int(row.get("user_id") or 0),
        event_id=int(row.get("event_id") or 0),
        slot_id=row.get("slot_id"),
        status=RegistrationStatus(row.get("status") or RegistrationStatus.CONFIRMED.value),
        notifications_enabled=bool(row.get("notifications_enabled") if row.get("notifications_enabled") is not None else True),
        created_at=_row_dt(row, "created_at") or utc_now(),
        updated_at=_row_dt(row, "updated_at") or utc_now(),
        canceled_at=_row_dt(row, "canceled_at"),
        attended_at=_row_dt(row, "attended_at"),
    )


def _notification(row) -> NotificationOutbox:
    return NotificationOutbox(
        id=int(row["id"]),
        event_id=int(row.get("event_id") or 0),
        registration_id=row.get("registration_id"),
        user_id=int(row.get("user_id") or 0),
        kind=NotificationKind(row.get("kind") or NotificationKind.REMINDER_1H.value),
        message_text=row.get("message_text") or "",
        send_after=_row_dt(row, "send_after") or utc_now(),
        status=OutboxStatus(row.get("status") or OutboxStatus.PENDING.value),
        attempts=int(row.get("attempts") or 0),
        last_error=row.get("last_error"),
        created_at=_row_dt(row, "created_at") or utc_now(),
        sent_at=_row_dt(row, "sent_at"),
    )


def _user_params(user: User) -> dict:
    return {
        "$user_id": _int(user.user_id),
        "$display_name": _utf8(user.display_name),
        "$is_bot": _bool(user.is_bot),
        "$created_at": _timestamp(user.created_at),
        "$updated_at": _timestamp(user.updated_at),
    }


def _consent_params(consent: Consent) -> dict:
    return {
        "$id": _int(consent.id),
        "$user_id": _int(consent.user_id),
        "$document_version": _utf8(consent.document_version),
        "$profile_data_allowed": _bool(consent.profile_data_allowed),
        "$created_at": _timestamp(consent.created_at),
    }


def _event_params(event: Event) -> dict:
    return {
        "$id": _int(event.id),
        "$title": _utf8(event.title),
        "$description": _utf8(event.description),
        "$requirements": _utf8(event.requirements),
        "$starts_at": _timestamp(event.starts_at),
        "$duration_minutes": _int(event.duration_minutes),
        "$format": _utf8(event.format.value),
        "$location_or_url": _utf8(event.location_or_url),
        "$cancellation_policy_text": _utf8(event.cancellation_policy_text),
        "$capacity_total": _int(event.capacity_total),
        "$registration_closed": _bool(event.registration_closed),
        "$late_cancel_policy": _utf8(event.late_cancel_policy.value),
        "$created_at": _timestamp(event.created_at),
        "$booked_count": _int(event.booked_count),
    }


def _slot_params(slot: EventSlot) -> dict:
    return {
        "$id": _int(slot.id),
        "$event_id": _int(slot.event_id),
        "$title": _utf8(slot.title),
        "$starts_at": _timestamp(slot.starts_at),
        "$ends_at": _timestamp(slot.ends_at),
        "$capacity": _int(slot.capacity),
        "$booked_count": _int(slot.booked_count),
    }


def _registration_params(registration: Registration) -> dict:
    return {
        "$id": _int(registration.id),
        "$code": _utf8(registration.code),
        "$user_id": _int(registration.user_id),
        "$event_id": _int(registration.event_id),
        "$slot_id": _opt_int(registration.slot_id),
        "$status": _utf8(registration.status.value),
        "$notifications_enabled": _bool(registration.notifications_enabled),
        "$created_at": _timestamp(registration.created_at),
        "$updated_at": _timestamp(registration.updated_at),
        "$canceled_at": _opt_timestamp(registration.canceled_at),
        "$attended_at": _opt_timestamp(registration.attended_at),
    }


def _notification_params(item: NotificationOutbox) -> dict:
    return {
        "$id": _int(item.id),
        "$event_id": _int(item.event_id),
        "$registration_id": _opt_int(item.registration_id),
        "$user_id": _int(item.user_id),
        "$kind": _utf8(item.kind.value),
        "$message_text": _utf8(item.message_text),
        "$send_after": _timestamp(item.send_after),
        "$status": _utf8(item.status.value),
        "$attempts": _int(item.attempts),
        "$last_error": _opt_utf8(item.last_error),
        "$created_at": _timestamp(item.created_at),
        "$sent_at": _opt_timestamp(item.sent_at),
    }


def _role_params(item: RoleAssignment) -> dict:
    return {
        "$id": _int(item.id),
        "$user_id": _int(item.user_id),
        "$role": _utf8(item.role),
        "$created_at": _opt_timestamp(item.created_at),
        "$created_by_user_id": _opt_int(item.created_by_user_id),
    }


def _organizer_event_params(item: OrganizerEvent) -> dict:
    return {"$id": _int(item.id), "$user_id": _int(item.user_id), "$event_id": _int(item.event_id)}


def _organizer_state_params(item: OrganizerState) -> dict:
    return {
        "$user_id": _int(item.user_id),
        "$mode": _utf8(item.mode),
        "$event_id": _opt_int(item.event_id),
        "$step": _utf8(item.step),
        "$data_json": _utf8(json.dumps(item.data, ensure_ascii=False)),
        "$updated_at": _timestamp(item.updated_at),
    }


def _audit_params(item: AuditLog) -> dict:
    return {
        "$id": _int(item.id),
        "$actor_user_id": _opt_int(item.actor_user_id),
        "$action": _utf8(item.action),
        "$entity_type": _utf8(item.entity_type),
        "$entity_id": _utf8(item.entity_id),
        "$metadata_json": _utf8(json.dumps(item.metadata_json, ensure_ascii=False)),
        "$created_at": _timestamp(item.created_at),
    }


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
