from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from app.bot.client import local_image_attachment


ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
IMAGE_TOKEN_MANIFEST_PATH = ASSETS_DIR / "max-image-tokens.json"


class BotImageAsset(StrEnum):
    MAIN_MENU = "main_menu"
    ORGANIZER_MENU = "organizer_menu"
    PARTICIPANTS_MENU = "participants_menu"
    NOTIFICATION_REMINDER = "notification_reminder"


ASSET_PATHS = {
    BotImageAsset.MAIN_MENU: ASSETS_DIR / "main-menu.png",
    BotImageAsset.ORGANIZER_MENU: ASSETS_DIR / "organizer-menu.png",
    BotImageAsset.PARTICIPANTS_MENU: ASSETS_DIR / "participants-menu.png",
    BotImageAsset.NOTIFICATION_REMINDER: ASSETS_DIR / "notification-reminder.png",
}


def image_attachment(asset: BotImageAsset | str):
    resolved_asset = BotImageAsset(asset)
    token = load_image_tokens().get(resolved_asset.value)
    if token:
        return {"type": "image", "payload": {"token": token}}
    return local_image_attachment(ASSET_PATHS[resolved_asset])


@lru_cache
def load_image_tokens() -> dict[str, str]:
    if not IMAGE_TOKEN_MANIFEST_PATH.exists():
        return {}
    try:
        manifest = json.loads(IMAGE_TOKEN_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        return {}
    tokens: dict[str, str] = {}
    for key, item in assets.items():
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or "").strip()
        if token:
            tokens[str(key)] = token
    return tokens

