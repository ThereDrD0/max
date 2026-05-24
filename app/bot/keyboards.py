from __future__ import annotations

from app.bot.payloads import Payload


def callback_button(
    text: str,
    payload: Payload | str,
    *,
    intent: str = "default",
) -> dict:
    packed = payload.pack() if isinstance(payload, Payload) else payload
    return {
        "type": "callback",
        "text": text,
        "payload": packed,
        "intent": intent,
    }


def link_button(text: str, url: str) -> dict:
    return {
        "type": "link",
        "text": text,
        "url": url,
    }


def clipboard_button(text: str, payload: str) -> dict:
    return {
        "type": "clipboard",
        "text": text,
        "payload": payload,
    }


def inline_keyboard(rows: list[list[dict]]) -> list[dict]:
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def consent_keyboard() -> list[dict]:
    return inline_keyboard(
        [[callback_button("✅ Согласен", Payload("consent_accept"))]]
    )
