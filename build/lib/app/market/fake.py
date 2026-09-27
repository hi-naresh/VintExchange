"""Deterministic reference prices for tests and the offline demo."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.domain.models import (
    ReferenceConfidence,
    ReferencePriceRecord,
    ReferenceSource,
    iso,
    utcnow,
)
from app.market.protocol import normalize_product_key
from app.repositories.seed import SEED_REFERENCES


def seed_reference(product_name: str,
                   seeds: dict[str, int] | None = None) -> ReferencePriceRecord | None:
    """Seeded fallback: exact key, else the longest seed whose words all appear."""
    seeds = SEED_REFERENCES if seeds is None else seeds
    key = normalize_product_key(product_name)
    words = set(key.split())
    match = key if key in seeds else None
    if match is None:
        subsets = [k for k in seeds if set(k.split()) <= words]
        match = max(subsets, key=len) if subsets else None
    if match is None:
        return None
    now = utcnow()
    return ReferencePriceRecord(
        id=f"seed:{match}", query_key=key, product_name=product_name,
        price_pence=seeds[match], source=ReferenceSource.SEED_FALLBACK,
        confidence=ReferenceConfidence.LOW, source_urls=[],
        fetched_at=iso(now), expires_at=iso(now + timedelta(days=3650)),
    )


class FakeMarketPriceProvider:
    """Seeded references by default; `references` overrides by normalized key."""

    def __init__(self, references: dict[str, ReferencePriceRecord] | None = None,
                 delay_seconds: float = 0.0) -> None:
        self.references = {normalize_product_key(k): v for k, v in (references or {}).items()}
        self.delay_seconds = delay_seconds
        self.calls = 0

    def _resolve(self, product_name: str) -> ReferencePriceRecord | None:
        override = self.references.get(normalize_product_key(product_name))
        return override if override is not None else seed_reference(product_name)

    async def lookup(self, product_name: str, country: str = "GB",
                     currency: str = "GBP") -> ReferencePriceRecord | None:
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self._resolve(product_name)

    async def peek(self, product_name: str, country: str = "GB",
                   currency: str = "GBP") -> ReferencePriceRecord | None:
        return self._resolve(product_name)
