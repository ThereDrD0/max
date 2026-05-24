from __future__ import annotations

from app.config import Settings
from app.storage.base import Storage
from app.storage.memory import MemoryStorage


def create_storage(settings: Settings) -> Storage:
    backend = settings.storage_backend.strip().lower()
    if backend == "memory":
        return MemoryStorage()
    if backend == "ydb":
        from app.storage.ydb import YdbStorage

        return YdbStorage(
            endpoint=settings.ydb_endpoint,
            database=settings.ydb_database,
            use_metadata_credentials=settings.ydb_metadata_credentials,
        )
    raise ValueError(f"Unsupported storage backend: {settings.storage_backend}")
