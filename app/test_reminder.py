from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from app.bot.client import MaxApiBotClient
from app.bootstrap import sync_roles_from_settings
from app.config import Settings, get_settings
from app.services.notification_worker import NotificationWorker
from app.services.organizer import OrganizerService
from app.storage.base import Storage
from app.storage.entities import NotificationOutbox
from app.storage.factory import create_storage


def format_simulated_starts_in(minutes: int) -> str:
    if minutes <= 0:
        raise ValueError("minutes must be greater than zero")
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} {_plural_ru(days, 'день', 'дня', 'дней')}"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')}"
    return f"{minutes} {_plural_ru(minutes, 'минута', 'минуты', 'минут')}"


def enqueue_test_reminders(
    storage: Storage,
    *,
    actor_user_id: int,
    event_id: int,
    slot_id: int | None,
    starts_in_minutes: int,
    now: Callable[[], datetime] | None = None,
) -> list[NotificationOutbox]:
    service = OrganizerService(storage, now=now)
    return service.enqueue_manual_reminder(
        actor_user_id,
        event_id,
        slot_id=slot_id,
        custom_text=None,
        starts_in_text=format_simulated_starts_in(starts_in_minutes),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    storage = create_storage(settings)
    sync_roles_from_settings(storage, settings)
    current = datetime.now(timezone.utc)
    created = enqueue_test_reminders(
        storage,
        actor_user_id=args.actor_user_id,
        event_id=args.event_id,
        slot_id=args.slot_id,
        starts_in_minutes=args.minutes,
        now=lambda: current,
    )
    print(f"Поставлено в очередь уведомлений: {len(created)}")
    if args.send_now:
        sent = asyncio.run(_send_due_now(storage, settings))
        print(f"Отправлено сразу: {sent}")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Поставить тестовое напоминание по мероприятию в очередь уведомлений.",
    )
    parser.add_argument("--actor-user-id", type=int, required=True)
    parser.add_argument("--event-id", type=int, required=True)
    parser.add_argument("--slot-id", type=int)
    parser.add_argument("--minutes", type=int, required=True)
    parser.add_argument(
        "--send-now",
        action="store_true",
        help="После постановки в очередь сразу запустить отправку due-уведомлений.",
    )
    return parser.parse_args(argv)


async def _send_due_now(storage: Storage, settings: Settings) -> int:
    worker = NotificationWorker(
        storage,
        MaxApiBotClient(settings.max_bot_token),
        max_rps=settings.max_api_rps,
        max_bot_username=settings.max_bot_username,
    )
    return await worker.process_due(limit=100)


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    value = abs(value)
    last_two = value % 100
    if 11 <= last_two <= 14:
        return many
    last = value % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


if __name__ == "__main__":
    raise SystemExit(main())
