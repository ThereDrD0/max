from __future__ import annotations

import json

import pytest

from app.bot import assets as bot_assets
from app.bot.assets import BotImageAsset, image_attachment


@pytest.fixture(autouse=True)
def clear_image_token_cache():
    bot_assets.load_image_tokens.cache_clear()
    yield
    bot_assets.load_image_tokens.cache_clear()


def test_image_attachment_uses_uploaded_token_manifest(tmp_path, monkeypatch):
    manifest_path = tmp_path / "max-image-tokens.json"
    manifest_path.write_text(
        json.dumps(
            {
                "assets": {
                    "main_menu": {
                        "path": "app/assets/main-menu.png",
                        "token": "main-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bot_assets, "IMAGE_TOKEN_MANIFEST_PATH", manifest_path)
    bot_assets.load_image_tokens.cache_clear()

    attachment = image_attachment(BotImageAsset.MAIN_MENU)

    assert attachment == {"type": "image", "payload": {"token": "main-token"}}


def test_image_attachment_falls_back_to_local_image_without_token(tmp_path, monkeypatch):
    manifest_path = tmp_path / "missing.json"
    monkeypatch.setattr(bot_assets, "IMAGE_TOKEN_MANIFEST_PATH", manifest_path)
    bot_assets.load_image_tokens.cache_clear()

    attachment = image_attachment(BotImageAsset.MAIN_MENU)

    assert getattr(attachment, "path", "").replace("\\", "/").endswith(
        "app/assets/main-menu.png"
    )
