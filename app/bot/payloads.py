from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Payload:
    action: str
    event_id: int | None = None
    slot_id: int | None = None
    registration_id: int | None = None
    value: str | None = None

    def pack(self) -> str:
        parts = [
            self.action,
            "" if self.event_id is None else str(self.event_id),
            "" if self.slot_id is None else str(self.slot_id),
            "" if self.registration_id is None else str(self.registration_id),
            "" if self.value is None else self.value,
        ]
        while parts and parts[-1] == "":
            parts.pop()
        return "|".join(parts)

    @classmethod
    def unpack(cls, raw: str) -> "Payload":
        parts = raw.split("|")
        action = parts[0]
        return cls(
            action=action,
            event_id=_int_or_none(parts, 1),
            slot_id=_int_or_none(parts, 2),
            registration_id=_int_or_none(parts, 3),
            value=parts[4] if len(parts) > 4 and parts[4] else None,
        )


def _int_or_none(parts: list[str], index: int) -> int | None:
    if len(parts) <= index or parts[index] == "":
        return None
    return int(parts[index])

