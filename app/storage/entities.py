from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.enums import (
    EventFormat,
    LateCancelPolicy,
    NotificationKind,
    OutboxStatus,
    RegistrationStatus,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class User:
    user_id: int
    display_name: str
    is_bot: bool = False
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Consent:
    id: int
    user_id: int
    document_version: str
    profile_data_allowed: bool = True
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class RoleAssignment:
    id: int
    user_id: int
    role: str


@dataclass(slots=True)
class EventSlot:
    id: int
    event_id: int
    title: str
    starts_at: datetime
    ends_at: datetime
    capacity: int
    booked_count: int = 0


@dataclass(slots=True)
class Event:
    id: int
    title: str
    description: str
    requirements: str
    starts_at: datetime
    duration_minutes: int
    format: EventFormat
    location_or_url: str
    cancellation_policy_text: str
    capacity_total: int
    registration_closed: bool = False
    late_cancel_policy: LateCancelPolicy = LateCancelPolicy.DENY
    created_at: datetime = field(default_factory=utc_now)
    booked_count: int = 0
    slots: list[EventSlot] = field(default_factory=list)
    image_token: str | None = None
    image_url: str | None = None


@dataclass(slots=True)
class OrganizerEvent:
    id: int
    user_id: int
    event_id: int


@dataclass(slots=True)
class OrganizerState:
    user_id: int
    mode: str
    event_id: int | None
    step: str
    data: dict
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Registration:
    id: int
    code: str
    user_id: int
    event_id: int
    slot_id: int | None
    status: RegistrationStatus = RegistrationStatus.CONFIRMED
    notifications_enabled: bool = True
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    canceled_at: datetime | None = None
    attended_at: datetime | None = None
    event: Event | None = None
    slot: EventSlot | None = None
    user: User | None = None


@dataclass(slots=True)
class NotificationOutbox:
    id: int
    event_id: int
    registration_id: int | None
    user_id: int
    kind: NotificationKind
    message_text: str
    send_after: datetime
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    sent_at: datetime | None = None
    registration: Registration | None = None


@dataclass(slots=True)
class AuditLog:
    id: int
    actor_user_id: int | None
    action: str
    entity_type: str
    entity_id: str
    metadata_json: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
