"""Forward exchange events to PostHog for funnels and dashboards.

Fire-and-forget: capture runs in a background task, has a short timeout, and a
failure is logged and dropped. Analytics can never block or fail a trade.
Payloads are the already-sanitized event payloads (no secrets, no prompts).
"""

from __future__ import annotations

import logging

import httpx

from app.domain.models import EventRecord

log = logging.getLogger(__name__)


class PostHogSink:
    def __init__(self, http: httpx.AsyncClient, api_key: str, host: str) -> None:
        self._http = http
        self._api_key = api_key
        self._url = host.rstrip("/") + "/i/v0/e/"
        self.sent = 0

    def __repr__(self) -> str:
        return f"PostHogSink(url={self._url!r})"

    async def capture(self, event: EventRecord) -> None:
        properties = {
            **event.payload,
            "stage": event.stage,
            "latency_ms": event.latency_ms,
            "request_id": event.request_id,
            "source": "trading-agentic-commerce",
            "$process_person_profile": False,
        }
        body = {
            "api_key": self._api_key,
            "event": f"gx_{event.type}",
            "distinct_id": event.request_id or "tac-system",
            "properties": properties,
            "timestamp": event.created_at,
        }
        try:
            response = await self._http.post(self._url, json=body, timeout=3)
            if response.status_code >= 400:
                log.warning("posthog capture HTTP %s", response.status_code)
                return
            self.sent += 1
        except httpx.HTTPError as error:
            log.warning("posthog capture failed: %s", type(error).__name__)
