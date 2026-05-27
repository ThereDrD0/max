from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


_CURRENT_TRACE: ContextVar["PerformanceTrace | None"] = ContextVar(
    "performance_trace",
    default=None,
)


@dataclass
class _MethodStats:
    count: int = 0
    ms: float = 0.0

    def add(self, elapsed_ms: float) -> None:
        self.count += 1
        self.ms += elapsed_ms

    def to_metric(self) -> dict[str, float | int]:
        return {"count": self.count, "ms": _round_ms(self.ms)}


@dataclass
class PerformanceTrace:
    source: str
    trigger: str
    enabled: bool = True
    slow_ms: int = 1000
    trace_id: str = field(default_factory=lambda: uuid4().hex[:16])
    started_at: float = field(default_factory=time.perf_counter)
    metadata: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    storage_methods: dict[str, _MethodStats] = field(default_factory=dict)
    ydb_methods: dict[str, _MethodStats] = field(default_factory=dict)
    max_methods: dict[str, _MethodStats] = field(default_factory=dict)
    input_media_count: int = 0

    def set_metadata(self, **values: Any) -> None:
        for key, value in values.items():
            if value is not None:
                self.metadata[key] = value

    def record_timing(self, name: str, elapsed_ms: float) -> None:
        self.timings[name] = self.timings.get(name, 0.0) + elapsed_ms

    def record_method(
        self,
        category: str,
        method: str,
        elapsed_ms: float,
        *,
        input_media_count: int = 0,
    ) -> None:
        methods = self._methods_for(category)
        stats = methods.setdefault(method, _MethodStats())
        stats.add(elapsed_ms)
        if category == "max":
            self.input_media_count += input_media_count

    def to_metric(
        self,
        *,
        ok: bool,
        status_code: int | None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        duration_ms = _elapsed_ms(self.started_at)
        return {
            "event": "perf_metric",
            "trace_id": self.trace_id,
            "source": self.source,
            "trigger": self.trigger,
            "update_type": self.metadata.get("update_type"),
            "action": self.metadata.get("action"),
            "ok": ok,
            "status_code": status_code,
            "error_type": error_type,
            "duration_ms": _round_ms(duration_ms),
            "slow": duration_ms >= self.slow_ms,
            "decode_ms": _round_ms(self.timings.get("decode", 0.0)),
            "dispatch_ms": _round_ms(self.timings.get("dispatch", 0.0)),
            "send_lock_wait_ms": _round_ms(
                self.timings.get("send_lock_wait", 0.0),
            ),
            "storage_calls": _total_count(self.storage_methods),
            "storage_ms": _round_ms(_total_ms(self.storage_methods)),
            "storage_methods": _methods_metric(self.storage_methods),
            "ydb_calls": _total_count(self.ydb_methods),
            "ydb_ms": _round_ms(_total_ms(self.ydb_methods)),
            "ydb_methods": _methods_metric(self.ydb_methods),
            "max_calls": _total_count(self.max_methods),
            "max_ms": _round_ms(_total_ms(self.max_methods)),
            "max_methods": _methods_metric(self.max_methods),
            "input_media_count": self.input_media_count,
        }

    def _methods_for(self, category: str) -> dict[str, _MethodStats]:
        if category == "storage":
            return self.storage_methods
        if category == "ydb":
            return self.ydb_methods
        if category == "max":
            return self.max_methods
        raise ValueError(f"Unsupported metric category: {category}")


@contextmanager
def performance_trace(
    *,
    source: str,
    trigger: str,
    enabled: bool = True,
    slow_ms: int = 1000,
):
    trace = PerformanceTrace(
        source=source,
        trigger=trigger,
        enabled=enabled,
        slow_ms=slow_ms,
    )
    token = _CURRENT_TRACE.set(trace)
    try:
        yield trace
    finally:
        _CURRENT_TRACE.reset(token)


def current_trace() -> PerformanceTrace | None:
    return _CURRENT_TRACE.get()


def set_trace_metadata(**values: Any) -> None:
    trace = current_trace()
    if trace is not None:
        trace.set_metadata(**values)


@contextmanager
def measure(name: str):
    started_at = time.perf_counter()
    try:
        yield
    finally:
        record_timing(name, _elapsed_ms(started_at))


def record_timing(name: str, elapsed_ms: float) -> None:
    trace = current_trace()
    if trace is not None:
        trace.record_timing(name, elapsed_ms)


def record_method(
    category: str,
    method: str,
    elapsed_ms: float,
    *,
    input_media_count: int = 0,
) -> None:
    trace = current_trace()
    if trace is not None:
        trace.record_method(
            category,
            method,
            elapsed_ms,
            input_media_count=input_media_count,
        )


def emit_perf_metric(
    trace: PerformanceTrace,
    *,
    ok: bool,
    status_code: int | None,
    error_type: str | None = None,
) -> None:
    if not trace.enabled:
        return
    print(
        json.dumps(
            trace.to_metric(
                ok=ok,
                status_code=status_code,
                error_type=error_type,
            ),
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


class MeasuredStorage:
    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        value = getattr(self._inner, name)
        if name.startswith("_") or not callable(value):
            return value

        def wrapper(*args, **kwargs):
            started_at = time.perf_counter()
            try:
                return value(*args, **kwargs)
            finally:
                record_method(
                    "storage",
                    name,
                    _elapsed_ms(started_at),
                )

        return wrapper


class MeasuredBotClient:
    def __init__(self, inner) -> None:
        self._inner = inner

    async def send_message(self, **kwargs):
        return await self._call("send_message", **kwargs)

    async def get_bot_username(self) -> str | None:
        method = getattr(self._inner, "get_bot_username", None)
        if method is None:
            return None
        return await self._call("get_bot_username")

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def _call(self, method_name: str, **kwargs):
        method = getattr(self._inner, method_name)
        input_media_count = _input_media_count(kwargs.get("attachments"))
        started_at = time.perf_counter()
        try:
            return await method(**kwargs)
        finally:
            record_method(
                "max",
                method_name,
                _elapsed_ms(started_at),
                input_media_count=input_media_count,
            )


def _input_media_count(attachments: list | None) -> int:
    classes = _input_media_classes()
    if not classes:
        return 0
    return sum(1 for item in attachments or [] if isinstance(item, classes))


def _input_media_classes() -> tuple[type, ...]:
    try:
        from maxapi.types.input_media import InputMedia, InputMediaBuffer
    except ImportError:
        try:
            from maxapi import InputMedia, InputMediaBuffer
        except ImportError:
            return ()
    return tuple(
        item
        for item in (InputMedia, InputMediaBuffer)
        if isinstance(item, type)
    )


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000


def _round_ms(value: float) -> float:
    return round(value, 3)


def _total_count(methods: dict[str, _MethodStats]) -> int:
    return sum(item.count for item in methods.values())


def _total_ms(methods: dict[str, _MethodStats]) -> float:
    return sum(item.ms for item in methods.values())


def _methods_metric(methods: dict[str, _MethodStats]) -> dict[str, dict[str, float | int]]:
    return {name: stats.to_metric() for name, stats in sorted(methods.items())}
