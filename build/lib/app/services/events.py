"""Timed stage/event recording.

Each stage measures monotonic elapsed time and records its event only after the
stage completes (in `finally`, so failed stages still expose latency plus the
sanitized error class). Payloads hold identifiers and structured facts only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from app.domain.models import EventCreate, EventRecord, Stage
from app.repositories.protocol import Repository

SECRET_KEYS = frozenset({"api_key", "authorization", "secret", "password", "token", "prompt"})


def sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if any(marker in key.lower() for marker in SECRET_KEYS):
            continue
        if isinstance(value, dict):
            clean[key] = sanitize(value)
        elif isinstance(value, str) and len(value) > 500:
            clean[key] = value[:500] + "…"
        else:
            clean[key] = value
    return clean


class EventSink(Protocol):
    async def capture(self, event: EventRecord) -> None: ...


class EventRecorder:
    def __init__(self, repository: Repository, sinks: list[EventSink] | None = None) -> None:
        self.repository = repository
        self.sinks = list(sinks or [])
        self._pending: set[asyncio.Task[None]] = set()

    async def emit(self, event_type: str, *, stage: Stage = "system",
                   request_id: str | None = None, latency_ms: float | None = None,
                   **payload: Any) -> EventRecord:
        record = await self.repository.create_event(EventCreate(
            type=event_type, stage=stage, request_id=request_id,
            payload=sanitize(payload), latency_ms=latency_ms,
        ))
        for sink in self.sinks:  # analytics are fire-and-forget
            task = asyncio.create_task(sink.capture(record))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        return record

    async def drain(self) -> None:
        """Wait for in-flight sink deliveries (tests, shutdown)."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

    @asynccontextmanager
    async def measure(self, stage: Stage, event_type: str,
                      request_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield a mutable payload dict; record the event with latency on exit."""
        payload: dict[str, Any] = {}
        started = time.monotonic_ns()
        try:
            yield payload
        except BaseException as error:
            payload["error"] = type(error).__name__
            raise
        finally:
            elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
            await self.emit(event_type, stage=stage, request_id=request_id,
                            latency_ms=round(elapsed_ms, 3), **payload)
