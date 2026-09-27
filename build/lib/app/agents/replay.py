"""Prompt-hash replay cache around any LLM client.

Key = SHA-256 over {operation, whitespace-normalized prompt, model}. Only
successful, validated responses are stored. Cache writes are atomic
(temporary file + replace). A miss with no live client fails with an actionable
error unless `fallback` is explicitly configured.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from app.agents.prompts import counter_messages, prompt_text, quote_messages, rfq_messages
from app.agents.protocol import (
    CounterProposal,
    LLMClient,
    Proposal,
    QuoteProposal,
    ReplayMiss,
    RFQProposal,
    parse_model,
)
from app.domain.models import InventoryRecord, MerchantRecord, QuoteRecord, RequestRecord

M = TypeVar("M", bound=Proposal)


def normalize_prompt(prompt: str) -> str:
    return " ".join(prompt.split())


def replay_key(operation: str, prompt: str, model: str) -> str:
    material = json.dumps({"operation": operation, "prompt": normalize_prompt(prompt),
                           "model": model}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


class ReplayLLMClient:
    def __init__(self, inner: LLMClient | None, cache_path: str | Path, *,
                 record: bool = False, fallback: LLMClient | None = None,
                 model: str | None = None) -> None:
        self.inner = inner
        self.cache_path = Path(cache_path)
        self.record = record
        self.fallback = fallback
        self.model_name = model or (inner.model_name if inner else "replay")
        self._lock = asyncio.Lock()
        self._cache: dict[str, dict[str, object]] | None = None

    def _load(self) -> dict[str, dict[str, object]]:
        if self._cache is None:
            if self.cache_path.exists():
                self._cache = json.loads(self.cache_path.read_text() or "{}")
            else:
                self._cache = {}
        return self._cache

    def _write_atomic(self, cache: dict[str, dict[str, object]]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache_path.parent, prefix=".replay-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(cache, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.cache_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def _call(self, operation: str, model: type[M], messages: tuple[str, str],
                    live: Callable[[LLMClient], Awaitable[M]]) -> M:
        key = replay_key(operation, prompt_text(messages), self.model_name)
        async with self._lock:
            hit = self._load().get(key)
        if hit is not None:
            return parse_model(model, json.dumps(hit), operation)

        client = self.inner or self.fallback
        if client is None:
            raise ReplayMiss(
                f"No replay entry for {operation} (key {key[:12]}). Record one with "
                "LLM_MODE=grok REPLAY_RECORD=true, or set REPLAY_FALLBACK=fake."
            )
        result = await live(client)
        if self.record and client is self.inner:
            async with self._lock:
                cache = self._load()
                cache[key] = result.model_dump(mode="json")
                self._write_atomic(cache)
        return result

    async def parse_rfq(self, text: str) -> RFQProposal:
        return await self._call("parse_rfq", RFQProposal, rfq_messages(text),
                                lambda c: c.parse_rfq(text))

    async def quote(self, merchant: MerchantRecord, inventory: list[InventoryRecord],
                    request: RequestRecord, round: int,
                    counter_pence: int | None = None) -> QuoteProposal:
        messages = quote_messages(merchant, inventory, request, round, counter_pence)
        return await self._call(
            "quote", QuoteProposal, messages,
            lambda c: c.quote(merchant, inventory, request, round, counter_pence),
        )

    async def counter(self, request: RequestRecord, quote: QuoteRecord,
                      round: int) -> CounterProposal:
        return await self._call("counter", CounterProposal,
                                counter_messages(request, quote, round),
                                lambda c: c.counter(request, quote, round))
