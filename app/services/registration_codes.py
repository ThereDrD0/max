from __future__ import annotations

import re
import secrets


REGISTRATION_CODE_V2_RE = re.compile(r"^\d{3}-\d{3}$")
MAX_USER_LINK_RE = re.compile(r"max://user/(\d+)")


def default_code_generator() -> str:
    digits = f"{secrets.randbelow(1_000_000):06d}"
    return f"{digits[:3]}-{digits[3:]}"


def normalize_registration_code_input(raw: str) -> str | None:
    value = (raw or "").strip()
    if REGISTRATION_CODE_V2_RE.fullmatch(value):
        return value
    if not re.fullmatch(r"[\d\s-]+", value):
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) != 6:
        return None
    return f"{digits[:3]}-{digits[3:]}"


def extract_max_user_id(raw: str) -> int | None:
    match = MAX_USER_LINK_RE.search(raw or "")
    return int(match.group(1)) if match else None
