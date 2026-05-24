from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.enums import EventFormat, LateCancelPolicy, NotificationKind, OutboxStatus, RegistrationStatus
from app.migration.snapshot import MigrationSnapshot
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


def export_legacy_database(source_database_url: str) -> MigrationSnapshot:
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(source_database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    snapshot = MigrationSnapshot()
    with engine.connect() as connection:
        if "users" in tables:
            snapshot.users = [
                User(
                    user_id=int(row["user_id"]),
                    display_name=row["display_name"],
                    is_bot=bool(row["is_bot"]),
                    created_at=_dt(row["created_at"]),
                    updated_at=_dt(row["updated_at"]),
                )
                for row in _rows(connection.execute(text("SELECT * FROM users")))
            ]
        if "events" in tables:
            snapshot.events = [
                Event(
                    id=int(row["id"]),
                    title=row["title"],
                    description=row["description"],
                    requirements=row["requirements"],
                    starts_at=_dt(row["starts_at"]),
                    duration_minutes=int(row["duration_minutes"]),
                    format=EventFormat(row["format"]),
                    location_or_url=row["location_or_url"],
                    cancellation_policy_text=row["cancellation_policy_text"],
                    capacity_total=int(row["capacity_total"]),
                    registration_closed=bool(row["registration_closed"]),
                    late_cancel_policy=LateCancelPolicy(row["late_cancel_policy"]),
                    created_at=_dt(row["created_at"]),
                )
                for row in _rows(connection.execute(text("SELECT * FROM events")))
            ]
        if "event_slots" in tables:
            snapshot.slots = [
                EventSlot(
                    id=int(row["id"]),
                    event_id=int(row["event_id"]),
                    title=row["title"],
                    starts_at=_dt(row["starts_at"]),
                    ends_at=_dt(row["ends_at"]),
                    capacity=int(row["capacity"]),
                )
                for row in _rows(connection.execute(text("SELECT * FROM event_slots")))
            ]
        if "consents" in tables:
            snapshot.consents = [
                Consent(
                    id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    document_version=row["document_version"],
                    profile_data_allowed=bool(row["profile_data_allowed"]),
                    created_at=_dt(row["created_at"]),
                )
                for row in _rows(connection.execute(text("SELECT * FROM consents")))
            ]
        if "role_assignments" in tables:
            snapshot.roles = [
                RoleAssignment(
                    id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    role=row["role"],
                )
                for row in _rows(connection.execute(text("SELECT * FROM role_assignments")))
            ]
        if "organizer_events" in tables:
            snapshot.organizer_events = [
                OrganizerEvent(
                    id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    event_id=int(row["event_id"]),
                )
                for row in _rows(connection.execute(text("SELECT * FROM organizer_events")))
            ]
        if "registrations" in tables:
            snapshot.registrations = [
                Registration(
                    id=int(row["id"]),
                    code=row["code"],
                    user_id=int(row["user_id"]),
                    event_id=int(row["event_id"]),
                    slot_id=int(row["slot_id"]) if row["slot_id"] is not None else None,
                    status=RegistrationStatus(row["status"]),
                    notifications_enabled=bool(row["notifications_enabled"]),
                    created_at=_dt(row["created_at"]),
                    updated_at=_dt(row["updated_at"]),
                    canceled_at=_dt_or_none(row["canceled_at"]),
                    attended_at=_dt_or_none(row["attended_at"]),
                )
                for row in _rows(connection.execute(text("SELECT * FROM registrations")))
            ]
        if "notification_outbox" in tables:
            snapshot.notifications = [
                NotificationOutbox(
                    id=int(row["id"]),
                    event_id=int(row["event_id"]),
                    registration_id=int(row["registration_id"]) if row["registration_id"] is not None else None,
                    user_id=int(row["user_id"]),
                    kind=NotificationKind(row["kind"]),
                    message_text=row["message_text"],
                    send_after=_dt(row["send_after"]),
                    status=OutboxStatus(row["status"]),
                    attempts=int(row["attempts"]),
                    last_error=row["last_error"],
                    created_at=_dt(row["created_at"]),
                    sent_at=_dt_or_none(row["sent_at"]),
                )
                for row in _rows(connection.execute(text("SELECT * FROM notification_outbox")))
            ]
        if "audit_log" in tables:
            snapshot.audit_logs = [
                AuditLog(
                    id=int(row["id"]),
                    actor_user_id=int(row["actor_user_id"]) if row["actor_user_id"] is not None else None,
                    action=row["action"],
                    entity_type=row["entity_type"],
                    entity_id=row["entity_id"],
                    metadata_json=_json(row["metadata_json"]),
                    created_at=_dt(row["created_at"]),
                )
                for row in _rows(connection.execute(text("SELECT * FROM audit_log")))
            ]
    return snapshot


def _rows(result) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result]


def _dt(raw) -> datetime:
    if isinstance(raw, datetime):
        value = raw
    else:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _dt_or_none(raw) -> datetime | None:
    if raw is None:
        return None
    return _dt(raw)


def _json(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)
