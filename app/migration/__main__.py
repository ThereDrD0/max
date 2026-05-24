from __future__ import annotations

from app.config import get_settings
from app.migration.legacy_sql import export_legacy_database
from app.migration.ydb_import import import_snapshot_to_storage, verify_snapshot_import
from app.storage.factory import create_storage


def main() -> None:
    settings = get_settings()
    if not settings.source_database_url:
        raise SystemExit("SOURCE_DATABASE_URL is required for migration")
    snapshot = export_legacy_database(settings.source_database_url)
    storage = create_storage(settings)
    import_snapshot_to_storage(snapshot, storage)
    report = verify_snapshot_import(snapshot, storage)
    if not report.ok:
        raise SystemExit(f"Migration verification failed: {report.errors}")
    print(f"Migration complete: {report.actual}")


if __name__ == "__main__":
    main()
