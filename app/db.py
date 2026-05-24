from __future__ import annotations

from app.config import Settings, get_settings
from app.storage.base import Storage
from app.storage.factory import create_storage


def create_app_storage(settings: Settings | None = None) -> Storage:
    return create_storage(settings or get_settings())
