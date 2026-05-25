from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.enums import NotificationKind, OutboxStatus, RegistrationStatus
from app.storage.entities import (
    Consent,
    Event,
    EventSlot,
    NotificationOutbox,
    OrganizerState,
    Registration,
    RoleAssignment,
    User,
)


ReminderRenderer = Callable[[NotificationKind, Event, Registration], str]
CodeGenerator = Callable[[], str]


class Storage(Protocol):
    def ready(self) -> bool: ...

    def upsert_user(
        self,
        user_id: int,
        display_name: str,
        *,
        is_bot: bool = False,
        now: datetime | None = None,
    ) -> User: ...

    def get_user(self, user_id: int) -> User | None: ...

    def get_last_bot_message_id(self, user_id: int) -> str | None: ...

    def set_last_bot_message_id(
        self,
        user_id: int,
        message_id: str | None,
        *,
        now: datetime | None = None,
    ) -> None: ...

    def record_profile_consent(
        self,
        user_id: int,
        document_version: str,
        *,
        now: datetime,
    ) -> Consent: ...

    def has_profile_consent(self, user_id: int) -> bool: ...

    def add_event(self, event: Event, *, slots: list[EventSlot] | None = None) -> Event: ...

    def get_event(
        self,
        event_id: int,
        *,
        with_slots: bool = True,
        with_image: bool = True,
    ) -> Event | None: ...

    def assign_event_slug(
        self,
        event_id: int,
        slug: str,
        *,
        now: datetime | None = None,
    ) -> None: ...

    def get_event_by_slug(self, slug: str) -> Event | None: ...

    def get_event_slug(self, event_id: int) -> str | None: ...

    def update_event_start(self, event_id: int, starts_at: datetime) -> None: ...

    def reschedule_event(
        self,
        actor_user_id: int,
        event_id: int,
        starts_at: datetime,
        *,
        now: datetime,
    ) -> Event: ...

    def update_event_location(
        self,
        actor_user_id: int,
        event_id: int,
        location_or_url: str,
        *,
        now: datetime,
    ) -> Event: ...

    def create_organizer_event(
        self,
        actor_user_id: int,
        event: Event,
        *,
        slots: list[EventSlot],
        image_token: str | None,
        image_url: str | None,
        now: datetime,
    ) -> Event: ...

    def replace_organizer_event(
        self,
        actor_user_id: int,
        event: Event,
        *,
        slots: list[EventSlot],
        image_token: str | None,
        image_url: str | None,
        now: datetime,
    ) -> Event: ...

    def set_event_image(
        self,
        actor_user_id: int,
        event_id: int,
        *,
        token: str | None,
        url: str | None,
        now: datetime,
    ) -> Event: ...

    def set_pending_event_image(
        self,
        user_id: int,
        event_id: int,
        *,
        now: datetime,
    ) -> None: ...

    def get_pending_event_image(self, user_id: int) -> int | None: ...

    def clear_pending_event_image(self, user_id: int) -> None: ...

    def list_events(
        self,
        *,
        starts_at_from: datetime | None = None,
        with_slots: bool = True,
        with_images: bool = True,
    ) -> list[Event]: ...

    def delete_expired_events(self, *, expired_before: datetime) -> int: ...

    def available_places(self, event_id: int, slot_id: int | None) -> int: ...

    def create_registration(
        self,
        *,
        user_id: int,
        event_id: int,
        slot_id: int | None,
        now: datetime,
        code_generator: CodeGenerator,
        render_reminder: ReminderRenderer,
    ) -> Registration: ...

    def cancel_registration(
        self,
        *,
        user_id: int,
        registration_id: int,
        now: datetime,
    ) -> Registration: ...

    def set_notifications_enabled(
        self,
        *,
        user_id: int,
        registration_id: int,
        enabled: bool,
        now: datetime,
    ) -> Registration: ...

    def get_registration(self, registration_id: int) -> Registration | None: ...

    def list_user_registrations(self, user_id: int) -> list[Registration]: ...

    def get_active_registration_for_event(
        self,
        user_id: int,
        event_id: int,
    ) -> Registration | None: ...

    def ensure_role(
        self,
        user_id: int,
        role: str,
        *,
        created_at: datetime | None = None,
        created_by_user_id: int | None = None,
    ) -> RoleAssignment: ...

    def get_role(self, user_id: int, role: str) -> RoleAssignment | None: ...

    def list_roles(self, role: str) -> list[RoleAssignment]: ...

    def has_role(self, user_id: int, role: str) -> bool: ...

    def delete_role(self, user_id: int, role: str) -> bool: ...

    def ensure_organizer_event(self, user_id: int, event_id: int) -> None: ...

    def list_organizer_events(
        self,
        actor_user_id: int,
        *,
        with_slots: bool = True,
        with_images: bool = True,
    ) -> list[Event]: ...

    def set_organizer_state(self, state: OrganizerState) -> OrganizerState: ...

    def get_organizer_state(self, user_id: int) -> OrganizerState | None: ...

    def clear_organizer_state(self, user_id: int) -> None: ...

    def get_event_registrations(
        self,
        actor_user_id: int,
        event_id: int,
    ) -> list[Registration]: ...

    def find_registration_by_code(
        self,
        actor_user_id: int,
        event_id: int,
        code: str,
    ) -> Registration: ...

    def find_registration_by_code_any_event(
        self,
        actor_user_id: int,
        code: str,
    ) -> Registration: ...

    def find_registration_by_code_global(self, code: str) -> Registration | None: ...

    def rewrite_registration_codes(self, code_generator: CodeGenerator) -> int: ...

    def close_registration(self, actor_user_id: int, event_id: int) -> Event: ...

    def mark_attended(
        self,
        actor_user_id: int,
        registration_id: int,
        *,
        now: datetime,
    ) -> Registration: ...

    def change_status(
        self,
        actor_user_id: int,
        registration_id: int,
        status: RegistrationStatus,
        *,
        now: datetime,
    ) -> Registration: ...

    def enqueue_manual_notification(
        self,
        *,
        actor_user_id: int,
        event_id: int,
        kind: NotificationKind,
        message_text: str,
        now: datetime,
    ) -> list[NotificationOutbox]: ...

    def add_notification(self, item: NotificationOutbox) -> NotificationOutbox: ...

    def sync_registration_reminders(
        self,
        *,
        now: datetime,
        render_reminder: ReminderRenderer,
    ) -> int: ...

    def list_notifications(self) -> list[NotificationOutbox]: ...

    def list_due_notifications(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[NotificationOutbox]: ...

    def set_notification_result(
        self,
        notification_id: int,
        *,
        status: OutboxStatus,
        now: datetime,
        error: str | None = None,
    ) -> None: ...

    def import_snapshot(self, snapshot) -> None: ...
