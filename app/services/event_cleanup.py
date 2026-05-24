from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.storage.base import Storage


ORGANIZER_EVENT_RETENTION_DAYS = 7
EVENT_CLEANUP_INTERVAL = timedelta(hours=6)


class EventCleanupService:
    def __init__(
        self,
        storage: Storage,
        *,
        now: Callable[[], datetime] | None = None,
        retention_days: int = ORGANIZER_EVENT_RETENTION_DAYS,
    ) -> None:
        self.storage = storage
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.retention_days = retention_days

    def cleanup(self, *, now: datetime | None = None) -> int:
        current = now or self.now()
        expired_before = current - timedelta(days=self.retention_days)
        return self.storage.delete_expired_events(expired_before=expired_before)
