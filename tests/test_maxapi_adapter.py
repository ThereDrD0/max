from __future__ import annotations

from pathlib import Path

import maxapi

from app.bot.client import (
    MaxApiBotClient,
    local_image_attachment,
    _adapt_attachments,
    _adapt_text_format,
)


def test_maxapi_adapter_loads_pip_installed_library():
    client = MaxApiBotClient("test-token")

    assert client.bot is not None
    assert maxapi.__file__ is not None
    assert Path("maxapi/maxapi").resolve() not in Path(maxapi.__file__).resolve().parents


def test_maxapi_adapter_wraps_dict_attachments_for_pip_library():
    attachments = [{"type": "inline_keyboard", "payload": {"buttons": []}}]

    adapted = _adapt_attachments(attachments)

    assert adapted is not None
    assert adapted[0].model_dump() == attachments[0]


def test_maxapi_adapter_converts_markdown_format_for_mentions():
    adapted = _adapt_text_format("markdown")

    assert adapted == maxapi.enums.TextFormat.MARKDOWN


def test_local_image_attachment_is_kept_as_maxapi_media(tmp_path):
    image_path = tmp_path / "menu.png"
    image_path.write_bytes(b"fake image")

    attachment = local_image_attachment(image_path)
    adapted = _adapt_attachments([attachment])

    assert getattr(attachment, "path", None) == str(image_path)
    assert adapted == [attachment]
