"""Repository-backed 24-hour cache in front of a live provider.

Resolution order: unexpired cached live result, then one bounded live call, then
the deterministic seeded fallback. Any timeout, rate limit, transport error,
malformed response, or weak evidence returns the fallback immediately; lookup
never raises and never blocks a fill. Concurrent lookups for the same canonical
product share a single in-flight live call.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app.domain.models import (
    ReferencePriceCreate,
    ReferencePriceRecord,
    ReferenceSource,
    iso,
    parse_timestamp,
    utcnow,
)
from app.market.fake import seed_reference
from app.market.protocol import MarketPriceProvider, normalize_product_key
from app.repositories.protocol import Repository

log = logging.getLogger(__name__)


class CachedMarketPriceProvider:
    def __init__(self, inner: MarketPriceProvider, repository: Repository, *,
                 ttl_hours: int = 24, timeout_seconds: float = 3.0,
                 seeds: dict[str, int] | None = None) -> None:
        self.inner = inner
        self.repository = repository
        self.ttl_hours = ttl_hours
        self.timeout_seconds = timeout_seconds
        self.seeds = seeds
        self.last_outcome: str | None = None
        self._inflight: dict[str, asyncio.Task[ReferencePriceRecord | None]] = {}

    async def _cached(self, key: str, country: str,
                      currency: str) -> ReferencePriceRecord | None:
        row = await self.repository.get_reference_price(key, country, currency)
        if row is None or row.source is ReferenceSource.SEED_FALLBACK:
            return None
        return row if parse_timestamp(row.expires_at) > utcnow() else None

    async def _live(self, product_name: str, key: str, country: str,
                    currency: str) -> ReferencePriceRecord | None:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                live = await self.inner.lookup(product_name, country, currency)
        except TimeoutError:
            self.last_outcome = "timeout"
            return None
        except Exception as error:  # noqa: BLE001 - lookup must never block a fill
            self.last_outcome = type(error).__name__
            log.warning("market lookup failed: %s", type(error).__name__)
            return None
        if live is None:
            self.last_outcome = "weak_evidence"
            return None
        now = utcnow()
        stored = await self.repository.upsert_reference_price(ReferencePriceCreate(
            query_key=key, product_name=product_name, country=country, currency=currency,
            price_pence=live.price_pence, source=ReferenceSource.TAVILY_LIVE,
            confidence=live.confidence, source_urls=live.source_urls,
            fetched_at=iso(now), expires_at=iso(now + timedelta(hours=self.ttl_hours)),
        ))
        self.last_outcome = "live"
        return stored

    async def lookup(self, product_name: str, country: str = "GB",
                     currency: str = "GBP") -> ReferencePriceRecord | None:
        key = normalize_product_key(product_name)
        cached = await self._cached(key, country, currency)
        if cached is not None:
            self.last_outcome = "cache"
            return cached.model_copy(update={"source": ReferenceSource.TAVILY_CACHE})

        flight_key = f"{key}|{country}|{currency}"
        task = self._inflight.get(flight_key)
        if task is None:
            task = asyncio.create_task(self._live(product_name, key, country, currency))
            self._inflight[flight_key] = task
            task.add_done_callback(lambda _: self._inflight.pop(flight_key, None))
        live = await asyncio.shield(task)
        return live if live is not None else seed_reference(product_name, self.seeds)

    async def peek(self, product_name: str, country: str = "GB",
                   currency: str = "GBP") -> ReferencePriceRecord | None:
        key = normalize_product_key(product_name)
        cached = await self._cached(key, country, currency)
        return cached if cached is not None else seed_reference(product_name, self.seeds)
