from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

from app.enums import OutboxStatus
from app.storage.base import Storage


class NotificationWorker:
    def __init__(
        self,
        storage: Storage,
        bot_client,
        *,
        now: Callable[[], datetime] | None = None,
        max_rps: int = 30,
    ) -> None:
        self.storage = storage
        self.bot_client = bot_client
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_rps = max(max_rps, 1)

    async def process_due(self, *, limit: int = 100) -> int:
        items = self.storage.list_due_notifications(now=self.now(), limit=limit)
        sent = 0
        delay = 1 / self.max_rps
        for item in items:
            registration = item.registration
            if registration is not None and not registration.notifications_enabled:
                self.storage.set_notification_result(
                    item.id,
                    status=OutboxStatus.SKIPPED,
                    now=self.now(),
                )
                continue
            try:
                await self.bot_client.send_message(
                    user_id=item.user_id,
                    text=item.message_text,
                )
            except Exception as exc:  # pragma: no cover - defensive edge
                self.storage.set_notification_result(
                    item.id,
                    status=OutboxStatus.FAILED,
                    now=self.now(),
                    error=str(exc),
                )
                continue
            self.storage.set_notification_result(
                item.id,
                status=OutboxStatus.SENT,
                now=self.now(),
            )
            sent += 1
            if delay > 0:
                await asyncio.sleep(delay)
        return sent
