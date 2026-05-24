from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from app.migration.legacy_sql import export_legacy_database
from app.migration.ydb_import import import_snapshot_to_storage, verify_snapshot_import


def test_migration_exports_legacy_sql_database_and_imports_to_storage(tmp_path, storage):
    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    is_bot BOOLEAN NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    requirements TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL,
                    format TEXT NOT NULL,
                    location_or_url TEXT NOT NULL,
                    cancellation_policy_text TEXT NOT NULL,
                    capacity_total INTEGER NOT NULL,
                    registration_closed BOOLEAN NOT NULL,
                    late_cancel_policy TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE registrations (
                    id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    slot_id INTEGER,
                    status TEXT NOT NULL,
                    notifications_enabled BOOLEAN NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    canceled_at TEXT,
                    attended_at TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO users VALUES
                (101, 'Анна', 0, :now, :now)
                """
            ),
            {"now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO events VALUES
                (1, 'День открытых дверей', 'Описание', 'Требования', :now, 60,
                 'online', 'https://example.edu', 'Отмена до начала', 10, 0,
                 'deny', :now)
                """
            ),
            {"now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO registrations VALUES
                (1, 'ABC123', 101, 1, NULL, 'confirmed', 1, :now, :now, NULL, NULL)
                """
            ),
            {"now": now},
        )

    snapshot = export_legacy_database(f"sqlite:///{db_path}")
    import_snapshot_to_storage(snapshot, storage)
    report = verify_snapshot_import(snapshot, storage)

    assert report.ok is True
    assert storage.get_user(101).display_name == "Анна"
    assert storage.find_registration_by_code_global("ABC123").event.title == "День открытых дверей"
