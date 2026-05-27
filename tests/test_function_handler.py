from __future__ import annotations

import asyncio
import json

from app.config import Settings
from app.function_handler import create_function_handler
from app.bot.payloads import Payload
from app.services.registration import RegistrationService
from app.storage.entities import NotificationOutbox
from app.enums import NotificationKind, OutboxStatus
from tests.conftest import create_event


def test_function_handler_processes_http_webhook(storage, fake_bot):
    handler = create_function_handler(
        Settings(webhook_secret="secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
    )

    response = handler(
        {
            "httpMethod": "POST",
            "headers": {"X-Max-Bot-Api-Secret": "secret"},
            "body": json.dumps(
                {
                    "update_type": "bot_started",
                    "chat_id": 9001,
                    "user": {"user_id": 101, "name": "Анна"},
                },
                ensure_ascii=False,
            ),
            "isBase64Encoded": False,
        },
        None,
    )

    assert response["statusCode"] == 200
    assert "командой хакатона" in fake_bot.sent[-1]["text"]


def test_function_handler_emits_webhook_perf_metric(storage, fake_bot, capsys):
    handler = create_function_handler(
        Settings(webhook_secret="secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
    )

    response = handler(
        {
            "httpMethod": "POST",
            "headers": {"X-Max-Bot-Api-Secret": "secret"},
            "body": json.dumps(
                {
                    "update_type": "bot_started",
                    "chat_id": 9001,
                    "user": {"user_id": 101, "name": "Анна"},
                },
                ensure_ascii=False,
            ),
            "isBase64Encoded": False,
        },
        None,
    )

    metrics = _perf_metrics(capsys.readouterr().out)
    assert response["statusCode"] == 200
    assert len(metrics) == 1
    metric = metrics[0]
    assert metric["event"] == "perf_metric"
    assert metric["source"] == "cloud_function"
    assert metric["trigger"] == "webhook"
    assert metric["update_type"] == "bot_started"
    assert metric["action"] == "bot_started"
    assert metric["ok"] is True
    assert metric["status_code"] == 200
    assert metric["duration_ms"] >= 0
    assert metric["decode_ms"] >= 0
    assert metric["dispatch_ms"] >= 0
    assert metric["storage_calls"] >= 1
    assert metric["max_calls"] >= 1
    assert "user_id" not in metric
    assert "chat_id" not in metric
    assert "text" not in metric


def test_function_handler_perf_metric_uses_callback_action(
    storage,
    fake_bot,
    fixed_now,
    capsys,
):
    create_event(storage, fixed_now, title="Пробное занятие по Python")
    storage.upsert_user(101, "Анна", now=fixed_now)
    storage.record_profile_consent(101, "docs", now=fixed_now)
    handler = create_function_handler(
        Settings(webhook_secret="secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
        now=lambda: fixed_now,
    )

    response = handler(
        {
            "httpMethod": "POST",
            "headers": {"X-Max-Bot-Api-Secret": "secret"},
            "body": json.dumps(
                {
                    "update_type": "message_callback",
                    "callback": {
                        "user": {"user_id": 101, "name": "Анна"},
                        "payload": Payload("catalog").pack(),
                    },
                    "message": {
                        "recipient": {"chat_id": 9001},
                        "body": {"mid": "mid.catalog"},
                    },
                },
                ensure_ascii=False,
            ),
            "isBase64Encoded": False,
        },
        None,
    )

    metric = _perf_metrics(capsys.readouterr().out)[0]
    assert response["statusCode"] == 200
    assert metric["update_type"] == "message_callback"
    assert metric["action"] == "catalog"


def test_function_handler_can_disable_perf_metrics(storage, fake_bot, capsys):
    handler = create_function_handler(
        Settings(
            webhook_secret="secret",
            max_bot_token="test-token",
            performance_metrics_enabled=False,
        ),
        storage=storage,
        bot_client=fake_bot,
    )

    response = handler(
        {
            "httpMethod": "POST",
            "headers": {"X-Max-Bot-Api-Secret": "secret"},
            "body": json.dumps(
                {
                    "update_type": "bot_started",
                    "chat_id": 9001,
                    "user": {"user_id": 101, "name": "Анна"},
                },
                ensure_ascii=False,
            ),
            "isBase64Encoded": False,
        },
        None,
    )

    assert response["statusCode"] == 200
    assert _perf_metrics(capsys.readouterr().out) == []


def test_function_handler_processes_deeplink_payload(storage, fake_bot, fixed_now):
    event = create_event(storage, fixed_now, title="День открытых дверей ИТ")
    storage.assign_event_slug(event.id, "it-open-day-2026-06-15", now=fixed_now)
    storage.upsert_user(101, "Анна", now=fixed_now)
    storage.record_profile_consent(101, "docs", now=fixed_now)
    handler = create_function_handler(
        Settings(
            webhook_secret="secret",
            max_bot_token="test-token",
            max_bot_username="id123_bot",
        ),
        storage=storage,
        bot_client=fake_bot,
        now=lambda: fixed_now,
    )

    response = handler(
        {
            "httpMethod": "POST",
            "headers": {"X-Max-Bot-Api-Secret": "secret"},
            "body": json.dumps(
                {
                    "update_type": "bot_started",
                    "chat_id": 9001,
                    "user": {"user_id": 101, "name": "Анна"},
                    "payload": "e_it-open-day-2026-06-15",
                },
                ensure_ascii=False,
            ),
            "isBase64Encoded": False,
        },
        None,
    )

    assert response["statusCode"] == 200
    assert "ℹ️ День открытых дверей ИТ" in fake_bot.sent[-1]["text"]
    assert "Ссылка: Нажмите чтобы скопировать" in fake_bot.sent[-1]["text"]
    assert (
        _clipboard_payload(fake_bot.sent[-1])
        == "https://max.ru/id123_bot?start=e_it-open-day-2026-06-15"
    )


def test_function_handler_passes_image_attachments_to_pending_event_image(
    storage,
    fake_bot,
    fixed_now,
):
    event = create_event(storage, fixed_now, title="Пробное занятие по Python")
    storage.ensure_role(501, "organizer")
    storage.ensure_organizer_event(501, event.id)
    storage.set_pending_event_image(501, event.id, now=fixed_now)
    handler = create_function_handler(
        Settings(webhook_secret="secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
    )

    response = handler(
        {
            "httpMethod": "POST",
            "headers": {"X-Max-Bot-Api-Secret": "secret"},
            "body": json.dumps(
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": 501, "name": "Организатор"},
                        "recipient": {"chat_id": 9003},
                        "body": {
                            "mid": "user-photo-mid",
                            "text": "",
                            "attachments": [
                                {
                                    "type": "image",
                                    "payload": {
                                        "token": "image-token",
                                        "url": "https://max.example/image.png",
                                    },
                                }
                            ],
                        },
                    },
                },
                ensure_ascii=False,
            ),
            "isBase64Encoded": False,
        },
        None,
    )

    stored_event = storage.get_event(event.id)
    assert response["statusCode"] == 200
    assert stored_event.image_token == "image-token"
    assert stored_event.image_url == "https://max.example/image.png"
    assert "Картинка обновлена" in fake_bot.sent[-1]["text"]
    assert fake_bot.deleted == []


def test_function_handler_reuses_open_event_loop_between_invocations(storage):
    class LoopBoundBot:
        def __init__(self) -> None:
            self.loop: asyncio.AbstractEventLoop | None = None
            self.calls = 0

        async def send_message(
            self,
            *,
            user_id=None,
            chat_id=None,
            text: str,
            attachments=None,
            notify=None,
        ):
            current_loop = asyncio.get_running_loop()
            if self.loop is None:
                self.loop = current_loop
            assert current_loop is self.loop
            assert not current_loop.is_closed()
            self.calls += 1
            return f"mid.{self.calls}"

    bot = LoopBoundBot()
    handler = create_function_handler(
        Settings(webhook_secret="secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=bot,
    )
    event = {
        "httpMethod": "POST",
        "headers": {"X-Max-Bot-Api-Secret": "secret"},
        "body": json.dumps(
            {
                "update_type": "bot_started",
                "chat_id": 9001,
                "user": {"user_id": 101, "name": "Анна"},
            },
            ensure_ascii=False,
        ),
        "isBase64Encoded": False,
    }

    assert handler(event, None)["statusCode"] == 200
    assert handler(event, None)["statusCode"] == 200


def test_function_handler_rejects_wrong_webhook_secret(storage, fake_bot):
    handler = create_function_handler(
        Settings(webhook_secret="secret", max_bot_token="test-token"),
        storage=storage,
        bot_client=fake_bot,
    )

    response = handler(
        {
            "httpMethod": "POST",
            "headers": {"X-Max-Bot-Api-Secret": "bad"},
            "body": "{}",
            "isBase64Encoded": False,
        },
        None,
    )

    assert response["statusCode"] == 403


def test_function_handler_timer_processes_notification_outbox(
    storage, fake_bot, fixed_now
):
    event = create_event(storage, fixed_now, title="Онлайн-консультация")
    service = RegistrationService(
        storage,
        now=lambda: fixed_now,
        code_generator=lambda: "TIMER1",
    )
    service.upsert_user(101, "Анна")
    service.record_profile_consent(101, "hackathon-2026-05")
    registration = service.create_registration(101, event.id, None)
    storage.add_notification(
        NotificationOutbox(
            id=0,
            event_id=event.id,
            registration_id=registration.id,
            user_id=101,
            kind=NotificationKind.VENUE_CHANGED,
            message_text="Проверьте аудиторию.",
            send_after=fixed_now,
        )
    )
    handler = create_function_handler(
        Settings(max_bot_token="test-token", max_api_rps=1000),
        storage=storage,
        bot_client=fake_bot,
        now=lambda: fixed_now,
    )

    response = handler(
        {
            "messages": [
                {
                    "event_metadata": {
                        "event_type": "yandex.cloud.events.serverless.triggers.TimerMessage"
                    }
                }
            ]
        },
        None,
    )

    assert response["statusCode"] == 200
    assert fake_bot.sent[-1]["text"] == "Проверьте аудиторию."
    assert storage.list_notifications()[-1].status == OutboxStatus.SENT


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
