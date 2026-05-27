from __future__ import annotations

import pytest

from app.bot.client import local_image_attachment
from app.observability.performance import (
    MeasuredBotClient,
    MeasuredStorage,
    current_trace,
    performance_trace,
)
from app.storage.ydb import YdbStorage


class _Bot:
    async def send_message(self, **kwargs):
        return "mid.send"

    async def get_bot_username(self):
        return "id123_bot"


@pytest.mark.asyncio
async def test_measured_bot_client_counts_max_calls_and_input_media(tmp_path):
    image_path = tmp_path / "menu.png"
    image_path.write_bytes(b"fake image")
    measured = MeasuredBotClient(_Bot())

    with performance_trace(source="test", trigger="webhook", enabled=True) as trace:
        await measured.send_message(
            user_id=101,
            text="hello",
            attachments=[local_image_attachment(image_path)],
        )
        assert await measured.get_bot_username() == "id123_bot"
        assert current_trace() is trace

    metric = trace.to_metric(ok=True, status_code=200)
    assert metric["max_calls"] == 2
    assert metric["input_media_count"] == 1
    assert metric["max_methods"]["send_message"]["count"] == 1
    assert metric["max_methods"]["get_bot_username"]["count"] == 1


def test_measured_storage_counts_public_storage_calls(storage, fixed_now):
    measured = MeasuredStorage(storage)

    with performance_trace(source="test", trigger="webhook", enabled=True) as trace:
        measured.upsert_user(101, "Анна", now=fixed_now)
        assert measured.has_profile_consent(101) is False

    metric = trace.to_metric(ok=True, status_code=200)
    assert metric["storage_calls"] == 2
    assert metric["storage_methods"]["upsert_user"]["count"] == 1
    assert metric["storage_methods"]["has_profile_consent"]["count"] == 1


def test_ydb_private_query_records_low_level_metric():
    class _Pool:
        def execute_with_retries(self, *args, **kwargs):
            return []

    storage = object.__new__(YdbStorage)
    storage.pool = _Pool()

    with performance_trace(source="test", trigger="webhook", enabled=True) as trace:
        assert storage._query("SELECT 1 AS ok;") == []

    metric = trace.to_metric(ok=True, status_code=200)
    assert metric["ydb_calls"] == 1
    assert metric["ydb_methods"]["query"]["count"] == 1
