from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class BotClient(Protocol):
    async def send_message(
        self,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        text: str,
        attachments: list | None = None,
        notify: bool | None = None,
        format: str | None = None,
    ) -> str | None:
        pass

    async def edit_message(
        self,
        *,
        message_id: str,
        text: str,
        attachments: list | None = None,
        notify: bool | None = None,
        format: str | None = None,
    ) -> str | None:
        pass

    async def delete_message(self, *, message_id: str) -> None:
        pass

    async def get_bot_username(self) -> str | None:
        pass


class MaxApiBotClient:
    def __init__(self, token: str, *, after_input_media_delay: float = 0.0) -> None:
        Bot = _load_maxapi_bot()
        self.bot = Bot(token=token)
        self.bot.after_input_media_delay = after_input_media_delay
        self._bot_username: str | None = None
        self._bot_username_loaded = False

    async def send_message(
        self,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        text: str,
        attachments: list | None = None,
        notify: bool | None = None,
        format: str | None = None,
    ) -> str | None:
        prepared_attachments = _adapt_attachments(attachments)
        result = await self.bot.send_message(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            attachments=prepared_attachments,
            notify=notify,
            format=_adapt_text_format(format),
        )
        return _extract_message_id(result)

    async def edit_message(
        self,
        *,
        message_id: str,
        text: str,
        attachments: list | None = None,
        notify: bool | None = None,
        format: str | None = None,
    ) -> str | None:
        prepared_attachments = _adapt_attachments(attachments)
        await self.bot.edit_message(
            message_id=message_id,
            text=text,
            attachments=prepared_attachments,
            notify=notify,
            format=_adapt_text_format(format),
        )
        return message_id

    async def delete_message(self, *, message_id: str) -> None:
        await self.bot.delete_message(message_id=message_id)

    async def get_bot_username(self) -> str | None:
        if self._bot_username_loaded:
            return self._bot_username
        user = await self.bot.get_me()
        username = getattr(user, "username", None)
        if username is None and isinstance(user, dict):
            username = user.get("username")
        self._bot_username = (
            str(username).strip().removeprefix("@") if username else None
        )
        self._bot_username_loaded = True
        return self._bot_username


@dataclass(slots=True)
class _RawAttachment:
    data: dict

    def model_dump(self) -> dict:
        return self.data


def _adapt_attachments(attachments: list | None) -> list | None:
    if attachments is None:
        return None
    return [
        _RawAttachment(item) if isinstance(item, dict) else item
        for item in attachments
    ]


def _adapt_text_format(format: str | None):
    if format is None:
        return None
    try:
        enums = importlib.import_module("maxapi.enums")
    except ImportError:
        return format
    enum_class = getattr(enums, "TextFormat", None) or getattr(enums, "ParseMode", None)
    if enum_class is None:
        return format
    try:
        return enum_class(format)
    except ValueError:
        return format


def local_image_attachment(path: str | Path):
    InputMedia = _load_maxapi_input_media()
    return InputMedia(str(path), type="image")


def _extract_message_id(result) -> str | None:
    if result is None:
        return None
    if isinstance(result, dict):
        message = result.get("message") or {}
        body = message.get("body") or {}
        mid = body.get("mid")
        return str(mid) if mid else None
    message = getattr(result, "message", None)
    body = getattr(message, "body", None)
    mid = getattr(body, "mid", None)
    return str(mid) if mid else None


def _load_maxapi_bot():
    module = importlib.import_module("maxapi")
    bot_class = getattr(module, "Bot", None)
    if bot_class is not None:
        return bot_class

    raise ImportError(
        "Не удалось импортировать Bot из библиотеки maxapi. "
        "Установите зависимость командой: python -m pip install maxapi[fastapi]"
    )


def _load_maxapi_input_media():
    module = importlib.import_module("maxapi")
    input_media_class = getattr(module, "InputMedia", None)
    if input_media_class is not None:
        return input_media_class

    module = importlib.import_module("maxapi.types.input_media")
    return getattr(module, "InputMedia")
