from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from secrets import compare_digest

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette import status

from app.bot.client import BotClient, MaxApiBotClient
from app.bot.dispatcher import dispatch_update
from app.bootstrap import sync_roles_from_settings
from app.config import Settings, get_settings
from app.services.event_cleanup import EventCleanupService
from app.storage.base import Storage
from app.storage.factory import create_storage


def create_app(
    settings: Settings | None = None,
    *,
    storage: Storage | None = None,
    bot_client: BotClient | None = None,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_storage = storage or create_storage(resolved_settings)
    resolved_bot_client = bot_client or MaxApiBotClient(resolved_settings.max_bot_token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        EventCleanupService(resolved_storage, now=now).cleanup()
        sync_roles_from_settings(resolved_storage, resolved_settings)
        yield

    app = FastAPI(title="MAX University Event Bot", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        if not resolved_storage.ready():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Storage is not ready",
            )
        return {"status": "ok"}

    @app.post(resolved_settings.webhook_path)
    async def webhook(request: Request) -> JSONResponse:
        _validate_secret(request.headers.get("X-Max-Bot-Api-Secret"), resolved_settings.webhook_secret)
        update = await request.json()
        await dispatch_update(
            storage=resolved_storage,
            bot_client=resolved_bot_client,
            settings=resolved_settings,
            update=update,
            now=now,
        )
        return JSONResponse({"ok": True})

    return app


def _validate_secret(incoming: str | None, webhook_secret: str) -> None:
    if not webhook_secret:
        return
    if incoming is None or not compare_digest(incoming, webhook_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )


def app_factory() -> FastAPI:
    return create_app()
