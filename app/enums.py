from __future__ import annotations

from enum import StrEnum


class EventFormat(StrEnum):
    ONLINE = "online"
    IN_PERSON = "in_person"


class LateCancelPolicy(StrEnum):
    DENY = "deny"
    ALLOW_LATE = "allow_late"


class RegistrationStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELED_BY_USER = "canceled_by_user"
    CANCELED_BY_ORGANIZER = "canceled_by_organizer"
    LATE_CANCELED = "late_canceled"
    ATTENDED = "attended"


class NotificationKind(StrEnum):
    TIME_CHANGED = "time_changed"
    VENUE_CHANGED = "venue_changed"
    JOIN_LINK_CHANGED = "join_link_changed"
    MANUAL_REMINDER = "manual_reminder"
    REMINDER_3D = "reminder_3d"
    REMINDER_24H = "reminder_24h"
    REMINDER_3H = "reminder_3h"
    REMINDER_START = "reminder_start"
    REMINDER_1H = "reminder_1h"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


ACTIVE_REGISTRATION_STATUSES = {
    RegistrationStatus.CONFIRMED,
    RegistrationStatus.ATTENDED,
}

MANUAL_NOTIFICATION_KINDS = {
    NotificationKind.TIME_CHANGED,
    NotificationKind.VENUE_CHANGED,
    NotificationKind.JOIN_LINK_CHANGED,
}
