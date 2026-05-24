from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from app.enums import EventFormat, LateCancelPolicy
from app.storage.entities import Event, EventSlot
from app.storage.memory import MemoryStorage


@pytest.fixture()
def fixed_now() -> datetime:
    return datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def storage() -> Iterator[MemoryStorage]:
    yield MemoryStorage()


def create_event(
    storage: MemoryStorage,
    fixed_now: datetime,
    *,
    title: str = "День открытых дверей ИТ",
    capacity: int = 2,
    starts_in: timedelta = timedelta(days=3),
    late_policy: LateCancelPolicy = LateCancelPolicy.DENY,
    with_slots: bool = False,
) -> Event:
    event = Event(
        id=0,
        title=title,
        description="Встреча с кафедрой и экскурсия по кампусу.",
        requirements="Паспортные данные не требуются.",
        starts_at=fixed_now + starts_in,
        duration_minutes=90,
        format=EventFormat.IN_PERSON,
        location_or_url="Главный корпус, аудитория 101",
        cancellation_policy_text="Отмена доступна до начала мероприятия.",
        capacity_total=capacity,
        late_cancel_policy=late_policy,
    )
    slots: list[EventSlot] = []
    if with_slots:
        slots = [
            EventSlot(
                id=0,
                event_id=0,
                title="10:00",
                starts_at=event.starts_at,
                ends_at=event.starts_at + timedelta(minutes=45),
                capacity=1,
            ),
            EventSlot(
                id=0,
                event_id=0,
                title="11:00",
                starts_at=event.starts_at + timedelta(hours=1),
                ends_at=event.starts_at + timedelta(hours=1, minutes=45),
                capacity=1,
            ),
        ]
    return storage.add_event(event, slots=slots)


class FakeBotClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[str] = []
        self._next_message_id = 1

    async def send_message(
        self,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        text: str,
        attachments: list | None = None,
        notify: bool | None = None,
    ) -> None:
        self.sent.append(
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "text": text,
                "attachments": attachments or [],
                "notify": notify,
            }
        )
        message_id = f"mid.{self._next_message_id}"
        self._next_message_id += 1
        return message_id

    async def edit_message(
        self,
        *,
        message_id: str,
        text: str,
        attachments: list | None = None,
        notify: bool | None = None,
    ) -> None:
        self.edited.append(
            {
                "message_id": message_id,
                "text": text,
                "attachments": attachments or [],
                "notify": notify,
            }
        )
        return message_id

    async def delete_message(self, *, message_id: str) -> None:
        self.deleted.append(message_id)


@pytest.fixture()
def fake_bot() -> FakeBotClient:
    return FakeBotClient()
