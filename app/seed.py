from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from app.bootstrap import sync_roles_from_settings
from app.config import Settings, get_settings
from app.db import create_app_storage
from app.enums import EventFormat, LateCancelPolicy
from app.storage.base import Storage
from app.storage.entities import Event, EventSlot


def load_seed_events(
    storage: Storage,
    path: Path,
    *,
    default_organizer_ids: list[int] | None = None,
) -> int:
    if not path.exists():
        return 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    created = 0
    existing = {
        (event.title, event.starts_at): event
        for event in storage.list_events()
    }
    for item in data.get("events", []):
        starts_at = _parse_datetime(item["starts_at"])
        event = existing.get((item["title"], starts_at))
        if event is None:
            event = Event(
                id=0,
                title=item["title"],
                description=item["description"],
                requirements=item["requirements"],
                starts_at=starts_at,
                duration_minutes=int(item["duration_minutes"]),
                format=EventFormat(item["format"]),
                location_or_url=item["location_or_url"],
                cancellation_policy_text=item["cancellation_policy_text"],
                capacity_total=int(item["capacity_total"]),
                late_cancel_policy=LateCancelPolicy(
                    item.get("late_cancel_policy", LateCancelPolicy.DENY.value)
                ),
            )
            slots = [
                EventSlot(
                    id=0,
                    event_id=0,
                    title=slot_item["title"],
                    starts_at=_parse_datetime(slot_item["starts_at"]),
                    ends_at=_parse_datetime(slot_item["ends_at"]),
                    capacity=int(slot_item["capacity"]),
                )
                for slot_item in item.get("slots", [])
            ]
            event = storage.add_event(event, slots=slots)
            organizer_ids = set(default_organizer_ids or [])
            organizer_ids.update(
                int(user_id) for user_id in item.get("organizer_user_ids", [])
            )
            for user_id in organizer_ids:
                storage.ensure_role(user_id, "organizer")
                storage.ensure_organizer_event(user_id, event.id)
            existing[(event.title, event.starts_at)] = event
            created += 1
        if item.get("slug"):
            storage.assign_event_slug(event.id, str(item["slug"]))
    return created


def bootstrap_database(settings: Settings | None = None) -> int:
    resolved = settings or get_settings()
    storage = create_app_storage(resolved)
    created = load_seed_events(
        storage,
        Path("seed/events.yaml"),
        default_organizer_ids=resolved.organizer_user_ids,
    )
    sync_roles_from_settings(storage, resolved)
    return created


def _parse_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


if __name__ == "__main__":
    count = bootstrap_database()
    print(f"Seed complete, created events: {count}")
