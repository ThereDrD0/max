from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

from app.bot.assets import BotImageAsset, image_attachment
from app.bot.deeplinks import (
    EVENT_PAYLOAD_PREFIX,
    MAX_START_PAYLOAD_LIMIT,
    build_default_event_slug,
    build_event_deeplink,
)
from app.bot.keyboards import callback_button, inline_keyboard, link_button
from app.bot.payloads import Payload
from app.domain import DuplicateEventSlugError
from app.enums import ACTIVE_REGISTRATION_STATUSES, OutboxStatus
from app.services.reminders import render_automatic_reminder
from app.storage.base import Storage
from app.storage.entities import Event, NotificationOutbox


class NotificationWorker:
    def __init__(
        self,
        storage: Storage,
        bot_client,
        *,
        now: Callable[[], datetime] | None = None,
        max_rps: int = 30,
        max_bot_username: str = "",
        reminder_sync_interval_minutes: int = 60,
        reminder_sync_window_minutes: int = 5,
    ) -> None:
        self.storage = storage
        self.bot_client = bot_client
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_rps = max(max_rps, 1)
        self.max_bot_username = max_bot_username
        self.reminder_sync_interval_minutes = max(reminder_sync_interval_minutes, 0)
        self.reminder_sync_window_minutes = max(reminder_sync_window_minutes, 0)
        self._bot_username: str | None = None
        self._bot_username_loaded = False

    async def process_due(self, *, limit: int = 100) -> int:
        current = self.now()
        if self._should_sync_reminders(current):
            self.storage.sync_registration_reminders(
                now=current,
                render_reminder=render_automatic_reminder,
            )
        items = self.storage.list_due_notifications(now=current, limit=limit)
        sent = 0
        delay = 1 / self.max_rps
        for item in items:
            registration = item.registration
            if registration is not None and (
                not registration.notifications_enabled
                or registration.status not in ACTIVE_REGISTRATION_STATUSES
            ):
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
                    attachments=await self._notification_attachments(item),
                )
            except Exception as exc:  # pragma: no cover - защитная ветка
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

    def _should_sync_reminders(self, now: datetime) -> bool:
        interval = self.reminder_sync_interval_minutes
        if interval <= 0:
            return False
        window = min(self.reminder_sync_window_minutes, interval)
        if window <= 0:
            return False
        minute_index = int(now.timestamp() // 60)
        return minute_index % interval < window

    async def _notification_attachments(self, item: NotificationOutbox) -> list | None:
        event = self._notification_event(item)
        if event is None:
            return None
        rows = [[await self._event_detail_button(event)]]
        return [
            image_attachment(BotImageAsset.NOTIFICATION_REMINDER),
            *inline_keyboard(rows),
        ]

    def _notification_event(self, item: NotificationOutbox) -> Event | None:
        if item.registration is not None and item.registration.event is not None:
            return item.registration.event
        return self.storage.get_event(item.event_id)

    async def _event_detail_button(self, event: Event) -> dict:
        deeplink = await self._event_deeplink(event)
        if deeplink:
            return link_button("ℹ️ Подробнее", deeplink)
        return callback_button("ℹ️ Подробнее", Payload("event_detail", event_id=event.id))

    async def _event_deeplink(self, event: Event) -> str | None:
        username = await self._max_bot_username()
        if not username:
            return None
        slug = self.storage.get_event_slug(event.id)
        if slug is None:
            slug = self._assign_default_event_slug(event)
        if slug is None:
            return None
        try:
            return build_event_deeplink(username, slug)
        except ValueError:
            return None

    async def _max_bot_username(self) -> str:
        configured = (self.max_bot_username or "").strip().removeprefix("@")
        if configured:
            return configured
        if self._bot_username_loaded:
            return self._bot_username or ""
        getter = getattr(self.bot_client, "get_bot_username", None)
        if getter is None:
            self._bot_username_loaded = True
            self._bot_username = ""
            return ""
        try:
            username = await getter()
        except Exception:
            username = ""
        self._bot_username = (username or "").strip().removeprefix("@")
        self._bot_username_loaded = True
        return self._bot_username

    def _assign_default_event_slug(self, event: Event) -> str | None:
        existing = self.storage.get_event_slug(event.id)
        if existing is not None:
            return existing
        base_slug = build_default_event_slug(event.title, event.starts_at)
        candidates = [base_slug, self._slug_with_suffix(base_slug, str(event.id))]
        for slug in dict.fromkeys(candidates):
            try:
                self.storage.assign_event_slug(event.id, slug, now=self.now())
                return slug
            except DuplicateEventSlugError:
                existing = self.storage.get_event_slug(event.id)
                if existing is not None:
                    return existing
        return self.storage.get_event_slug(event.id)

    @staticmethod
    def _slug_with_suffix(slug: str, suffix: str) -> str:
        max_slug_length = MAX_START_PAYLOAD_LIMIT - len(EVENT_PAYLOAD_PREFIX)
        suffix = suffix.strip("-") or "event"
        max_base_length = max_slug_length - len(suffix) - 1
        if max_base_length <= 0:
            return slug[:max_slug_length].strip("-")
        return f"{slug[:max_base_length].strip('-')}-{suffix}"
