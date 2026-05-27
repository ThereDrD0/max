from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.web import create_app
from tests.conftest import create_event


def test_webhook_rejects_wrong_secret(storage, fake_bot):
    app = create_app(
        Settings(webhook_secret="right-secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
    )
    client = TestClient(app)

    response = client.post(
        "/webhook",
        json={"update_type": "bot_started"},
        headers={"X-Max-Bot-Api-Secret": "wrong-secret"},
    )

    assert response.status_code == 403


def test_webhook_accepts_bot_started_update(storage, fake_bot):
    app = create_app(
        Settings(webhook_secret="right-secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
    )
    client = TestClient(app)

    response = client.post(
        "/webhook",
        json={
            "update_type": "bot_started",
            "chat_id": 9001,
            "user": {"user_id": 101, "name": "Анна", "is_bot": False},
        },
        headers={"X-Max-Bot-Api-Secret": "right-secret"},
    )

    assert response.status_code == 200
    assert fake_bot.sent[-1]["user_id"] == 101
    assert "командой хакатона" in fake_bot.sent[-1]["text"]


def test_webhook_emits_fastapi_perf_metric(storage, fake_bot, capsys):
    app = create_app(
        Settings(webhook_secret="right-secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
    )
    client = TestClient(app)

    response = client.post(
        "/webhook",
        json={
            "update_type": "bot_started",
            "chat_id": 9001,
            "user": {"user_id": 101, "name": "Анна", "is_bot": False},
        },
        headers={"X-Max-Bot-Api-Secret": "right-secret"},
    )

    metrics = _perf_metrics(capsys.readouterr().out)
    assert response.status_code == 200
    assert len(metrics) == 1
    assert metrics[0]["source"] == "fastapi"
    assert metrics[0]["trigger"] == "webhook"
    assert metrics[0]["action"] == "bot_started"


def test_webhook_keeps_message_created_source_message(storage, fake_bot, fixed_now):
    create_event(storage, fixed_now, title="Пробное занятие по Python")
    storage.upsert_user(101, "Анна", now=fixed_now)
    storage.record_profile_consent(101, "docs", now=fixed_now)
    app = create_app(
        Settings(webhook_secret="right-secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
        now=lambda: fixed_now,
    )
    client = TestClient(app)

    response = client.post(
        "/webhook",
        json={
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 101, "name": "Анна", "is_bot": False},
                "recipient": {"chat_id": 9001},
                "body": {"text": "/events", "mid": "mid.user-command"},
            },
        },
        headers={"X-Max-Bot-Api-Secret": "right-secret"},
    )

    assert response.status_code == 200
    assert "Пробное занятие по Python" in fake_bot.sent[-1]["text"]
    assert fake_bot.deleted == []


def test_webhook_accepts_bot_started_deeplink_payload(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="День открытых дверей ИТ")
    storage.assign_event_slug(event.id, "it-open-day-2026-06-15", now=fixed_now)
    storage.upsert_user(101, "Анна", now=fixed_now)
    storage.record_profile_consent(101, "docs", now=fixed_now)
    app = create_app(
        Settings(
            webhook_secret="right-secret",
            max_bot_token="test-token",
            max_bot_username="id123_bot",
        ),
        storage=storage,
        bot_client=fake_bot,
        now=lambda: fixed_now,
    )
    client = TestClient(app)

    response = client.post(
        "/webhook",
        json={
            "update_type": "bot_started",
            "chat_id": 9001,
            "user": {"user_id": 101, "name": "Анна", "is_bot": False},
            "payload": "e_it-open-day-2026-06-15",
        },
        headers={"X-Max-Bot-Api-Secret": "right-secret"},
    )

    assert response.status_code == 200
    assert "ℹ️ День открытых дверей ИТ" in fake_bot.sent[-1]["text"]
    assert "Ссылка: Нажмите чтобы скопировать" in fake_bot.sent[-1]["text"]
    assert (
        _clipboard_payload(fake_bot.sent[-1])
        == "https://max.ru/id123_bot?start=e_it-open-day-2026-06-15"
    )


def test_health_and_ready_endpoints(storage, fake_bot):
    app = create_app(
        Settings(webhook_secret="right-secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
    )
    client = TestClient(app)

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ok"}


def _clipboard_payload(message: dict) -> str | None:
    for attachment in message["attachments"]:
        for row in attachment["payload"]["buttons"]:
            for button in row:
                if button.get("type") == "clipboard":
                    return button.get("payload")
    return None


def _perf_metrics(output: str) -> list[dict]:
    metrics = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("event") == "perf_metric":
            metrics.append(item)
    return metrics
