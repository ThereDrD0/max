from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from datetime import datetime, timezone
from secrets import compare_digest
from threading import RLock

from app.bot.client import BotClient, MaxApiBotClient
from app.bot.dispatcher import dispatch_update
from app.bootstrap import sync_roles_from_settings
from app.config import Settings, get_settings
from app.observability.performance import (
    MeasuredBotClient,
    MeasuredStorage,
    emit_perf_metric,
    measure,
    performance_trace,
)
from app.services.event_cleanup import EVENT_CLEANUP_INTERVAL, EventCleanupService
from app.services.notification_worker import NotificationWorker
from app.storage.base import Storage
from app.storage.factory import create_storage


def create_function_handler(
    settings: Settings | None = None,
    *,
    storage: Storage | None = None,
    bot_client: BotClient | None = None,
    now: Callable[[], datetime] | None = None,
):
    resolved_settings = settings or get_settings()
    resolved_storage = storage or create_storage(resolved_settings)
    resolved_bot_client = bot_client or MaxApiBotClient(resolved_settings.max_bot_token)
    async_runner = _AsyncRunner()
    cleanup_scheduler = _CleanupScheduler(resolved_storage, now=now)
    cleanup_scheduler.run(force=True)
    sync_roles_from_settings(resolved_storage, resolved_settings)

    def handler(event, context):
        if _is_timer_event(event):
            with performance_trace(
                source="cloud_function",
                trigger="timer",
                enabled=resolved_settings.performance_metrics_enabled,
                slow_ms=resolved_settings.performance_metrics_slow_ms,
            ) as trace:
                status_code: int | None = None
                error_type: str | None = None
                try:
                    response = async_runner.run(
                        _handle_timer(
                            resolved_storage,
                            resolved_bot_client,
                            resolved_settings,
                            cleanup_scheduler,
                            now,
                        )
                    )
                    status_code = int(response.get("statusCode") or 200)
                    return response
                except Exception as exc:
                    error_type = type(exc).__name__
                    raise
                finally:
                    emit_perf_metric(
                        trace,
                        ok=status_code is not None and status_code < 400,
                        status_code=status_code,
                        error_type=error_type,
                    )
        with performance_trace(
            source="cloud_function",
            trigger="webhook",
            enabled=resolved_settings.performance_metrics_enabled,
            slow_ms=resolved_settings.performance_metrics_slow_ms,
        ) as trace:
            status_code: int | None = None
            error_type: str | None = None
            try:
                response = _run_async(
                    _handle_http(
                        event,
                        storage=resolved_storage,
                        bot_client=resolved_bot_client,
                        settings=resolved_settings,
                        now=now,
                    ),
                    async_runner,
                )
                status_code = int(response.get("statusCode") or 200)
                return response
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                emit_perf_metric(
                    trace,
                    ok=status_code is not None and status_code < 400,
                    status_code=status_code,
                    error_type=error_type,
                )

    return handler


async def _handle_http(
    event: dict,
    *,
    storage: Storage,
    bot_client: BotClient,
    settings: Settings,
    now: Callable[[], datetime] | None,
) -> dict:
    method = (event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    if method == "GET":
        return _response(200, {"status": "ok"})
    if method != "POST":
        return _response(405, {"error": "Method not allowed"})
    headers = _normalize_headers(event.get("headers") or {})
    incoming_secret = headers.get("x-max-bot-api-secret")
    if settings.webhook_secret and (
        incoming_secret is None or not compare_digest(incoming_secret, settings.webhook_secret)
    ):
        return _response(403, {"error": "Forbidden"})
    with measure("decode"):
        update = _decode_body(event)
    with measure("dispatch"):
        await dispatch_update(
            storage=MeasuredStorage(storage),
            bot_client=MeasuredBotClient(bot_client),
            settings=settings,
            update=update,
            now=now,
        )
    return _response(200, {"ok": True})


async def _handle_timer(
    storage: Storage,
    bot_client: BotClient,
    settings: Settings,
    cleanup_scheduler: "_CleanupScheduler",
    now: Callable[[], datetime] | None,
) -> dict:
    storage = MeasuredStorage(storage)
    bot_client = MeasuredBotClient(bot_client)
    removed_events = cleanup_scheduler.run(storage=storage)
    worker = NotificationWorker(
        storage,
        bot_client,
        now=now,
        max_rps=settings.max_api_rps,
        max_bot_username=settings.max_bot_username,
        reminder_sync_interval_minutes=settings.reminder_sync_interval_minutes,
        reminder_sync_window_minutes=settings.reminder_sync_window_minutes,
    )
    sent = await worker.process_due(limit=100)
    return _response(200, {"ok": True, "sent": sent, "removed_events": removed_events})


def _is_timer_event(event: dict) -> bool:
    messages = event.get("messages")
    if not isinstance(messages, list):
        return False
    return any(
        (message.get("event_metadata") or {}).get("event_type")
        == "yandex.cloud.events.serverless.triggers.TimerMessage"
        for message in messages
        if isinstance(message, dict)
    )


def _decode_body(event: dict) -> dict:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    if isinstance(body, dict):
        return body
    return json.loads(body)


def _normalize_headers(headers: dict) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "isBase64Encoded": False,
        "body": json.dumps(body, ensure_ascii=False),
    }


class _AsyncRunner:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = RLock()

    def run(self, coro):
        with self._lock:
            loop = self._get_loop()
            return loop.run_until_complete(coro)

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        return self._loop


class _CleanupScheduler:
    def __init__(
        self,
        storage: Storage,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._last_run: datetime | None = None
        self._lock = RLock()

    def run(self, *, force: bool = False, storage: Storage | None = None) -> int:
        current = self.now()
        with self._lock:
            if (
                not force
                and self._last_run is not None
                and current - self._last_run < EVENT_CLEANUP_INTERVAL
            ):
                return 0
            self._last_run = current
        return EventCleanupService(storage or self.storage, now=self.now).cleanup(
            now=current,
        )


def _run_async(coro, runner: _AsyncRunner | None = None):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return (runner or _AsyncRunner()).run(coro)
    return loop.run_until_complete(coro)
