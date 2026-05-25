from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv
from maxapi import Bot
from maxapi.types import InputMedia


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "app" / "assets" / "max-image-tokens.json"


@dataclass(frozen=True, slots=True)
class AssetToUpload:
    key: str
    path: Path


@dataclass(frozen=True, slots=True)
class UploadedAsset:
    key: str
    path: Path
    token: str


DEFAULT_ASSETS = (
    AssetToUpload("main_menu", ROOT / "app" / "assets" / "main-menu.png"),
    AssetToUpload("organizer_menu", ROOT / "app" / "assets" / "organizer-menu.png"),
    AssetToUpload("participants_menu", ROOT / "app" / "assets" / "participants-menu.png"),
    AssetToUpload(
        "notification_reminder",
        ROOT / "app" / "assets" / "notification-reminder.png",
    ),
)


def build_manifest(
    uploaded_assets: Sequence[UploadedAsset],
    *,
    generated_at: datetime,
) -> dict:
    return {
        "generated_at": generated_at.isoformat(),
        "assets": {
            item.key: {
                "path": _display_path(item.path),
                "token": item.token,
            }
            for item in uploaded_assets
        },
    }


async def upload_assets(bot: Bot, assets: Sequence[AssetToUpload]) -> list[UploadedAsset]:
    uploaded: list[UploadedAsset] = []
    for asset in assets:
        upload = await bot.upload_media(InputMedia(path=str(asset.path), type="image"))
        token = str(upload.payload.token).strip()
        if not token:
            raise RuntimeError(f"MAX вернул пустой token для {asset.path}")
        uploaded.append(UploadedAsset(asset.key, asset.path, token))
    return uploaded


def parse_asset(raw: str) -> AssetToUpload:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("Формат должен быть KEY=PATH")
    key, path = raw.split("=", 1)
    clean_key = key.strip()
    if not clean_key:
        raise argparse.ArgumentTypeError("KEY не должен быть пустым")
    resolved_path = Path(path.strip())
    if not resolved_path.is_absolute():
        resolved_path = ROOT / resolved_path
    return AssetToUpload(clean_key, resolved_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Загрузить картинки бота в MAX и сохранить token-ы для повторного использования."
    )
    parser.add_argument(
        "--asset",
        action="append",
        type=parse_asset,
        help="Картинка в формате KEY=PATH. Если не указано, грузятся стандартные картинки бота.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Куда записать JSON с token-ами.",
    )
    parser.add_argument(
        "--token-env",
        default="MAX_BOT_TOKEN",
        help="Имя переменной окружения с token-ом бота.",
    )
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Не читать .env перед запуском.",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")
    token = os.getenv(args.token_env)
    if not token:
        raise SystemExit(f"Не найден token бота в переменной {args.token_env}")
    assets = tuple(args.asset or DEFAULT_ASSETS)
    for asset in assets:
        if not asset.path.exists():
            raise SystemExit(f"Файл не найден: {asset.path}")
    bot = Bot(token=token)
    try:
        uploaded = await upload_assets(bot, assets)
    finally:
        await bot.close_session()
    manifest = build_manifest(uploaded, generated_at=datetime.now(timezone.utc))
    output = args.output
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for item in uploaded:
        print(f"{item.key}: {item.token}")
    print(f"JSON записан: {_display_path(output)}")


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    asyncio.run(async_main())
