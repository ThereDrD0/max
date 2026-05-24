from __future__ import annotations

from app.migration.snapshot import MigrationReport, MigrationSnapshot
from app.storage.base import Storage


def import_snapshot_to_storage(snapshot: MigrationSnapshot, storage: Storage) -> None:
    storage.import_snapshot(snapshot)


def verify_snapshot_import(
    snapshot: MigrationSnapshot,
    storage: Storage,
) -> MigrationReport:
    expected = {
        "users": len(snapshot.users),
        "events": len(snapshot.events),
        "registrations": len(snapshot.registrations),
        "notifications": len(snapshot.notifications),
    }
    actual = {
        "users": sum(1 for user in snapshot.users if storage.get_user(user.user_id) is not None),
        "events": sum(1 for event in snapshot.events if storage.get_event(event.id) is not None),
        "registrations": sum(
            1
            for registration in snapshot.registrations
            if storage.get_registration(registration.id) is not None
        ),
        "notifications": len(storage.list_notifications()),
    }
    errors = [
        f"{name}: expected {expected[name]}, got {actual[name]}"
        for name in expected
        if expected[name] != actual[name]
    ]
    return MigrationReport(ok=not errors, expected=expected, actual=actual, errors=errors)
