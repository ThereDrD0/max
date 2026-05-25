from __future__ import annotations

import os

import ydb

from app.config import Settings, get_settings


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id Int64 NOT NULL,
        display_name Utf8,
        is_bot Bool,
        created_at Timestamp,
        updated_at Timestamp,
        PRIMARY KEY (user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS consents (
        id Int64 NOT NULL,
        user_id Int64,
        document_version Utf8,
        profile_data_allowed Bool,
        created_at Timestamp,
        INDEX idx_consents_user GLOBAL ON (user_id),
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_sessions (
        user_id Int64 NOT NULL,
        last_bot_message_id Utf8,
        updated_at Timestamp,
        PRIMARY KEY (user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id Int64 NOT NULL,
        title Utf8,
        description Utf8,
        requirements Utf8,
        starts_at Timestamp,
        duration_minutes Int64,
        format Utf8,
        location_or_url Utf8,
        cancellation_policy_text Utf8,
        capacity_total Int64,
        registration_closed Bool,
        late_cancel_policy Utf8,
        created_at Timestamp,
        booked_count Int64,
        INDEX idx_events_starts_at GLOBAL ON (starts_at),
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_slots (
        id Int64 NOT NULL,
        event_id Int64,
        title Utf8,
        starts_at Timestamp,
        ends_at Timestamp,
        capacity Int64,
        booked_count Int64,
        INDEX idx_slots_event GLOBAL ON (event_id),
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_deeplinks (
        slug Utf8 NOT NULL,
        event_id Int64,
        created_at Timestamp,
        INDEX idx_event_deeplinks_event GLOBAL ON (event_id),
        PRIMARY KEY (slug)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_images (
        event_id Int64 NOT NULL,
        token Utf8,
        url Utf8,
        updated_by_user_id Int64,
        updated_at Timestamp,
        PRIMARY KEY (event_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_event_images (
        user_id Int64 NOT NULL,
        event_id Int64,
        created_at Timestamp,
        PRIMARY KEY (user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS organizer_states (
        user_id Int64 NOT NULL,
        mode Utf8,
        event_id Int64,
        step Utf8,
        data_json Utf8,
        updated_at Timestamp,
        PRIMARY KEY (user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS role_assignments (
        id Int64 NOT NULL,
        user_id Int64,
        role Utf8,
        created_at Timestamp,
        created_by_user_id Int64,
        INDEX idx_roles_user GLOBAL ON (user_id),
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS organizer_events (
        id Int64 NOT NULL,
        user_id Int64,
        event_id Int64,
        INDEX idx_organizer_events_user GLOBAL ON (user_id),
        INDEX idx_organizer_events_event GLOBAL ON (event_id),
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS registrations (
        id Int64 NOT NULL,
        code Utf8,
        user_id Int64,
        event_id Int64,
        slot_id Int64,
        status Utf8,
        notifications_enabled Bool,
        created_at Timestamp,
        updated_at Timestamp,
        canceled_at Timestamp,
        attended_at Timestamp,
        INDEX idx_registrations_code GLOBAL ON (code),
        INDEX idx_registrations_user GLOBAL ON (user_id),
        INDEX idx_registrations_event GLOBAL ON (event_id),
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS registration_codes (
        code Utf8 NOT NULL,
        registration_id Int64,
        PRIMARY KEY (code)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS active_registration_keys (
        active_key Utf8 NOT NULL,
        registration_id Int64,
        PRIMARY KEY (active_key)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_outbox (
        id Int64 NOT NULL,
        event_id Int64,
        registration_id Int64,
        user_id Int64,
        kind Utf8,
        message_text Utf8,
        send_after Timestamp,
        status Utf8,
        attempts Int64,
        last_error Utf8,
        created_at Timestamp,
        sent_at Timestamp,
        INDEX idx_outbox_status_send GLOBAL ON (status, send_after),
        INDEX idx_outbox_registration GLOBAL ON (registration_id),
        PRIMARY KEY (id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id Int64 NOT NULL,
        actor_user_id Int64,
        action Utf8,
        entity_type Utf8,
        entity_id Utf8,
        metadata_json Utf8,
        created_at Timestamp,
        INDEX idx_audit_entity GLOBAL ON (entity_type, entity_id),
        PRIMARY KEY (id)
    );
    """,
]

ROLE_ASSIGNMENT_COLUMNS = {
    "created_at": "Timestamp",
    "created_by_user_id": "Int64",
}


def create_driver(settings: Settings | None = None) -> ydb.Driver:
    resolved = settings or get_settings()
    credentials = _credentials(resolved)
    driver = ydb.Driver(
        endpoint=resolved.ydb_endpoint,
        database=resolved.ydb_database,
        credentials=credentials,
    )
    driver.wait(timeout=10, fail_fast=True)
    return driver


def ensure_schema(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    with create_driver(resolved) as driver:
        pool = ydb.QuerySessionPool(driver, size=10)
        for statement in SCHEMA_STATEMENTS:
            pool.execute_with_retries(
                statement,
                retry_settings=ydb.RetrySettings(idempotent=True),
            )
        _ensure_role_assignment_columns(driver, pool, resolved)


def _ensure_role_assignment_columns(
    driver: ydb.Driver,
    pool: ydb.QuerySessionPool,
    settings: Settings,
) -> None:
    existing_columns = _table_columns(driver, settings, "role_assignments")
    for column_name, column_type in ROLE_ASSIGNMENT_COLUMNS.items():
        if column_name in existing_columns:
            continue
        pool.execute_with_retries(
            f"ALTER TABLE role_assignments ADD COLUMN {column_name} {column_type};",
            retry_settings=ydb.RetrySettings(idempotent=True),
        )


def _table_columns(
    driver: ydb.Driver,
    settings: Settings,
    table_name: str,
) -> set[str]:
    table_path = f"{settings.ydb_database.rstrip('/')}/{table_name}"
    description = driver.table_client.describe_table(table_path)
    return {column.name for column in description.columns}


def _credentials(settings: Settings):
    if os.getenv("YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS") or os.getenv("YDB_ACCESS_TOKEN_CREDENTIALS"):
        return ydb.credentials_from_env_variables()
    if settings.ydb_metadata_credentials:
        os.environ["YDB_METADATA_CREDENTIALS"] = "1"
        return ydb.credentials_from_env_variables()
    return ydb.AnonymousCredentials()


if __name__ == "__main__":
    ensure_schema()
    print("YDB schema is ready")
