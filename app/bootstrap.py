from __future__ import annotations

from app.config import Settings
from app.storage.base import Storage


def sync_roles_from_settings(storage: Storage, settings: Settings) -> None:
    for user_id in settings.admin_user_ids:
        storage.ensure_role(user_id, "admin")
    for user_id in settings.organizer_user_ids:
        storage.ensure_role(user_id, "organizer")
        for event in storage.list_events():
            storage.ensure_organizer_event(user_id, event.id)
