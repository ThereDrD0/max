from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scripts import upload_max_assets


def test_build_manifest_keeps_asset_paths_and_tokens():
    generated_at = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)

    manifest = upload_max_assets.build_manifest(
        [
            upload_max_assets.UploadedAsset(
                key="main_menu",
                path=Path("app/assets/main-menu.png"),
                token="main-token",
            )
        ],
        generated_at=generated_at,
    )

    assert manifest == {
        "generated_at": "2026-05-25T12:00:00+00:00",
        "assets": {
            "main_menu": {
                "path": "app/assets/main-menu.png",
                "token": "main-token",
            }
        },
    }


def test_default_assets_cover_shared_bot_images():
    keys = {asset.key for asset in upload_max_assets.DEFAULT_ASSETS}

    assert keys == {
        "main_menu",
        "organizer_menu",
        "participants_menu",
        "notification_reminder",
    }

