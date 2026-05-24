from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo

from app.enums import NotificationKind
from app.storage.entities import Event, EventSlot, Registration


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
AUTOMATIC_REMINDER_KINDS = (
    NotificationKind.REMINDER_3D,
    NotificationKind.REMINDER_24H,
    NotificationKind.REMINDER_3H,
    NotificationKind.REMINDER_START,
)
LEGACY_AUTOMATIC_REMINDER_KINDS = (NotificationKind.REMINDER_1H,)
AUTOMATIC_REMINDER_OFFSETS = {
    NotificationKind.REMINDER_3D: timedelta(days=3),
    NotificationKind.REMINDER_24H: timedelta(days=1),
    NotificationKind.REMINDER_3H: timedelta(hours=3),
    NotificationKind.REMINDER_START: timedelta(0),
}


def automatic_reminder_schedule(
    event: Event,
    registration: Registration,
) -> list[tuple[NotificationKind, datetime]]:
    starts_at = reminder_starts_at(event, registration)
    return [
        (kind, starts_at - AUTOMATIC_REMINDER_OFFSETS[kind])
        for kind in AUTOMATIC_REMINDER_KINDS
    ]


def reminder_starts_at(event: Event, registration: Registration) -> datetime:
    slot = reminder_slot(event, registration)
    return slot.starts_at if slot is not None else event.starts_at


def reminder_slot(event: Event, registration: Registration) -> EventSlot | None:
    if registration.slot is not None:
        return registration.slot
    if registration.slot_id is None:
        return None
    return next((slot for slot in event.slots if slot.id == registration.slot_id), None)


def render_automatic_reminder(
    kind: NotificationKind,
    event: Event,
    registration: Registration,
) -> str:
    return render_reminder_message(
        event,
        registration,
        starts_in_text=_automatic_starts_in_text(kind),
    )


def render_manual_reminder(
    event: Event | None,
    registration: Registration,
    *,
    now: datetime,
    custom_text: str | None,
    starts_in_text: str | None = None,
) -> str:
    resolved_event = event or registration.event
    relative = starts_in_text
    if relative is None and resolved_event is not None:
        relative = format_remaining(reminder_starts_at(resolved_event, registration), now)
    return render_reminder_message(
        resolved_event,
        registration,
        starts_in_text=relative,
        custom_text=custom_text,
    )


def render_reminder_message(
    event: Event | None,
    registration: Registration,
    *,
    starts_in_text: str | None,
    custom_text: str | None = None,
) -> str:
    clean_custom_text = (custom_text or "").strip()
    starts_in = _normalize_starts_in_text(starts_in_text)
    title = event.title if event is not None else "Мероприятие"
    lines = ["🔔 Напоминание о мероприятии"]
    if event is not None:
        starts_at = reminder_starts_at(event, registration)
        lines.append(f"Начало: {format_moscow_datetime(starts_at)} ({starts_in})")
    if clean_custom_text:
        lines.extend(["", clean_custom_text])
    lines.extend(["", title])
    slot = reminder_slot(event, registration) if event is not None else registration.slot
    if slot is not None:
        lines.append(f"Слот: {slot.title}")
    lines.append(f"Код записи: {registration.code}")
    if event is not None:
        lines.append(f"Место/ссылка: {event.location_or_url}")
    return "\n".join(lines)


def format_moscow_datetime(value: datetime) -> str:
    return value.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")


def format_remaining(starts_at: datetime, now: datetime) -> str:
    total_seconds = (starts_at - now).total_seconds()
    if total_seconds <= 0:
        return "сейчас"
    total_minutes = max(1, ceil(total_seconds / 60))
    days, day_remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(day_remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} {_plural_ru(days, 'день', 'дня', 'дней')}")
        if hours:
            parts.append(f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')}")
    elif hours:
        parts.append(f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')}")
        if minutes:
            parts.append(f"{minutes} {_plural_ru(minutes, 'минута', 'минуты', 'минут')}")
    else:
        parts.append(f"{minutes} {_plural_ru(minutes, 'минута', 'минуты', 'минут')}")
    return f"через {' '.join(parts)}"


def _automatic_starts_in_text(kind: NotificationKind) -> str:
    if kind == NotificationKind.REMINDER_3D:
        return "через 3 дня"
    if kind == NotificationKind.REMINDER_24H:
        return "через 24 часа"
    if kind == NotificationKind.REMINDER_3H:
        return "через 3 часа"
    if kind == NotificationKind.REMINDER_START:
        return "сейчас"
    return "через 1 час"


def _normalize_starts_in_text(value: str | None) -> str:
    clean = (value or "").strip()
    if not clean:
        return "сейчас"
    if clean == "сейчас" or clean.startswith("через "):
        return clean
    return f"через {clean}"


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    value = abs(value)
    last_two = value % 100
    if 11 <= last_two <= 14:
        return many
    last = value % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many
