from __future__ import annotations

from dataclasses import dataclass, field

from app.storage.entities import (
    AuditLog,
    Consent,
    Event,
    EventSlot,
    NotificationOutbox,
    OrganizerEvent,
    Registration,
    RoleAssignment,
    User,
)


@dataclass(slots=True)
class MigrationSnapshot:
    users: list[User] = field(default_factory=list)
    consents: list[Consent] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    slots: list[EventSlot] = field(default_factory=list)
    roles: list[RoleAssignment] = field(default_factory=list)
    organizer_events: list[OrganizerEvent] = field(default_factory=list)
    registrations: list[Registration] = field(default_factory=list)
    notifications: list[NotificationOutbox] = field(default_factory=list)
    audit_logs: list[AuditLog] = field(default_factory=list)


@dataclass(slots=True)
class MigrationReport:
    ok: bool
    expected: dict[str, int]
    actual: dict[str, int]
    errors: list[str] = field(default_factory=list)
