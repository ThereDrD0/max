from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.enums import NotificationKind
from app.domain import SlotNotFoundError
from app.services.registration_codes import (
    default_code_generator,
    extract_max_user_id,
    normalize_registration_code_input,
)
from app.services.reminders import render_automatic_reminder
from app.storage.base import Storage
from app.storage.entities import Consent, Event, Registration, User


CodeGenerator = Callable[[], str]


class RegistrationService:
    def __init__(
        self,
        storage: Storage,
        *,
        now: Callable[[], datetime] | None = None,
        code_generator: CodeGenerator | None = None,
    ) -> None:
        self.storage = storage
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.code_generator = code_generator or default_code_generator

    def upsert_user(
        self,
        user_id: int,
        display_name: str,
        *,
        is_bot: bool = False,
    ) -> User:
        return self.storage.upsert_user(
            user_id,
            display_name,
            is_bot=is_bot,
            now=self.now(),
        )

    def touch_user(
        self,
        user_id: int,
        display_name: str,
        *,
        is_bot: bool = False,
    ) -> None:
        self.storage.touch_user(
            user_id,
            display_name,
            is_bot=is_bot,
            now=self.now(),
        )

    def record_profile_consent(
        self,
        user_id: int,
        document_version: str,
    ) -> Consent:
        return self.storage.record_profile_consent(
            user_id,
            document_version,
            now=self.now(),
        )

    def has_profile_consent(self, user_id: int) -> bool:
        return self.storage.has_profile_consent(user_id)

    def list_events(self) -> list[Event]:
        current = self.now()
        return [
            event
            for event in self.storage.list_events(
                starts_at_from=current,
                with_slots=True,
                with_images=False,
            )
            if event.starts_at > current
        ]

    def available_places(self, event_id: int, slot_id: int | None) -> int:
        return self.storage.available_places(event_id, slot_id)

    def available_places_for_event(
        self,
        event: Event,
        slot_id: int | None = None,
    ) -> int:
        if event.slots:
            if slot_id is None:
                return sum(
                    max(slot.capacity - slot.booked_count, 0)
                    for slot in event.slots
                )
            for slot in event.slots:
                if slot.id == slot_id:
                    return max(slot.capacity - slot.booked_count, 0)
            raise SlotNotFoundError("Слот не найден")
        return max(event.capacity_total - event.booked_count, 0)

    def create_registration(
        self,
        user_id: int,
        event_id: int,
        slot_id: int | None,
    ) -> Registration:
        return self.storage.create_registration(
            user_id=user_id,
            event_id=event_id,
            slot_id=slot_id,
            now=self.now(),
            code_generator=self.code_generator,
            render_reminder=self._render_reminder,
        )

    def cancel_registration(self, user_id: int, registration_id: int) -> Registration:
        return self.storage.cancel_registration(
            user_id=user_id,
            registration_id=registration_id,
            now=self.now(),
        )

    def set_notifications_enabled(
        self,
        user_id: int,
        registration_id: int,
        *,
        enabled: bool,
    ) -> Registration:
        return self.storage.set_notifications_enabled(
            user_id=user_id,
            registration_id=registration_id,
            enabled=enabled,
            now=self.now(),
        )

    def list_user_registrations(
        self,
        user_id: int,
        *,
        with_event_slots: bool = True,
        with_slot: bool = True,
        with_user: bool = True,
        with_images: bool = True,
    ) -> list[Registration]:
        return self.storage.list_user_registrations(
            user_id,
            with_event_slots=with_event_slots,
            with_slot=with_slot,
            with_user=with_user,
            with_images=with_images,
        )

    @staticmethod
    def _render_reminder(
        kind: NotificationKind,
        event: Event,
        registration: Registration,
    ) -> str:
        return render_automatic_reminder(kind, event, registration)
