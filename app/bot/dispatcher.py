from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from app.bot.client import BotClient
from app.bot.handlers import BotHandlers
from app.bot.payloads import Payload
from app.config import Settings
from app.observability.performance import set_trace_metadata
from app.storage.base import Storage


async def dispatch_update(
    *,
    storage: Storage,
    bot_client: BotClient,
    settings: Settings,
    update: dict,
    now: Callable[[], datetime] | None = None,
) -> None:
    handlers = BotHandlers(
        storage,
        bot_client,
        now=now,
        documents_version=settings.documents_version,
        app_env=settings.app_env,
        max_bot_username=settings.max_bot_username,
        organizer_config_user_ids=settings.organizer_user_ids,
    )
    update_type = update.get("update_type")
    set_trace_metadata(
        update_type=str(update_type) if update_type else None,
        action=_update_action(update),
    )
    if update_type == "bot_started":
        user = update.get("user") or {}
        await handlers.handle_bot_started(
            user_id=int(user.get("user_id")),
            display_name=_display_name(user),
            chat_id=update.get("chat_id"),
            start_payload=update.get("payload"),
        )
        return
    if update_type == "message_created":
        message = update.get("message") or {}
        sender = message.get("sender") or {}
        recipient = message.get("recipient") or {}
        body = message.get("body") or {}
        await handlers.handle_message(
            user_id=int(sender.get("user_id")),
            display_name=_display_name(sender),
            chat_id=recipient.get("chat_id"),
            text=body.get("text") or "",
            source_message_id=body.get("mid"),
            attachments=body.get("attachments") or [],
        )
        return
    if update_type == "message_callback":
        callback = update.get("callback") or {}
        user = callback.get("user") or {}
        message = update.get("message") or {}
        recipient = message.get("recipient") or {}
        await handlers.handle_callback(
            user_id=int(user.get("user_id")),
            display_name=_display_name(user),
            chat_id=recipient.get("chat_id"),
            payload=callback.get("payload") or "",
            source_message_id=(message.get("body") or {}).get("mid"),
        )


def _display_name(user: dict) -> str:
    return (
        user.get("name")
        or " ".join(
            item for item in [user.get("first_name"), user.get("last_name")] if item
        )
        or f"Пользователь {user.get('user_id')}"
    )


def _update_action(update: dict) -> str:
    update_type = str(update.get("update_type") or "")
    if update_type == "bot_started":
        return "bot_started"
    if update_type == "message_created":
        message = update.get("message") or {}
        body = message.get("body") or {}
        text = str(body.get("text") or "").strip()
        command = text.split(maxsplit=1)[0] if text else ""
        return command if command.startswith("/") else "message_created"
    if update_type == "message_callback":
        callback = update.get("callback") or {}
        try:
            return Payload.unpack(str(callback.get("payload") or "")).action
        except (IndexError, TypeError, ValueError):
            return "callback_unknown"
    return update_type or "unknown"
