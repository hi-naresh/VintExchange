"""Bounded Tavily Search adapter with deterministic GBP extraction.

One POST per lookup (fast search, five results, UK), wrapped in a hard timeout.
Prices are extracted with a regex, never by an LLM. Used/refurbished/renewed and
monthly-payment results are discarded, implausible outliers are dropped, one
price is kept per registrable domain, and the median (half-up) is taken. Two or
more distinct domains give high confidence; fewer give low confidence. Raw
response bodies are never persisted.
"""

from __future__ import annotations

import asyncio
import re
import statistics
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from app.domain.models import (
    ReferenceConfidence,
    ReferencePriceRecord,
    ReferenceSource,
    iso,
    utcnow,
)
from app.market.protocol import normalize_product_key

TAVILY_URL = "https://api.tavily.com/search"
_GBP = re.compile(r"£\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")
_REJECT = re.compile(r"\b(used|refurbished|renewed|pre-owned|monthly|per month)\b",
                     re.IGNORECASE)
_MULTI_PART_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "co.nz"}
MIN_PLAUSIBLE_PENCE = 500
MAX_PLAUSIBLE_PENCE = 5_000_000
OUTLIER_LOW, OUTLIER_HIGH = 0.5, 2.0


def extract_gbp_prices(text: str) -> list[int]:
    prices = []
    for pounds, pence in _GBP.findall(text):
        value = int(pounds.replace(",", "")) * 100 + int(pence or 0)
        if MIN_PLAUSIBLE_PENCE <= value <= MAX_PLAUSIBLE_PENCE:
            prices.append(value)
    return prices


def registrable_domain(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return None
    labels = host.split(".")
    if ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def median_half_up(values: list[int]) -> int:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid] + 1) // 2


def parse_reference(product_name: str, results: list[dict[str, Any]],
                    ttl_hours: int = 24) -> ReferencePriceRecord | None:
    candidates: list[tuple[str, str, int]] = []  # (domain, url, price)
    for result in results:
        title = str(result.get("title") or "")
        content = str(result.get("content") or "")
        url = str(result.get("url") or "")
        text = f"{title} {content}"
        domain = registrable_domain(url)
        if domain is None or _REJECT.search(text):
            continue
        prices = extract_gbp_prices(text)
        if prices:
            candidates.append((domain, url, prices[0]))
    if not candidates:
        return None

    centre = statistics.median(p for _, _, p in candidates)
    plausible = [c for c in candidates if OUTLIER_LOW * centre <= c[2] <= OUTLIER_HIGH * centre]
    per_domain: dict[str, tuple[str, int]] = {}
    for domain, url, price in plausible:
        per_domain.setdefault(domain, (url, price))
    if not per_domain:
        return None

    now = utcnow()
    confidence = (ReferenceConfidence.HIGH if len(per_domain) >= 2
                  else ReferenceConfidence.LOW)
    key = normalize_product_key(product_name)
    return ReferencePriceRecord(
        id=f"live:{key}", query_key=key, product_name=product_name,
        price_pence=median_half_up([price for _, price in per_domain.values()]),
        source=ReferenceSource.TAVILY_LIVE, confidence=confidence,
        source_urls=[url for url, _ in per_domain.values()],
        fetched_at=iso(now), expires_at=iso(now + timedelta(hours=ttl_hours)),
    )


class TavilyMarketPriceProvider:
    def __init__(self, http_client: httpx.AsyncClient, api_key: SecretStr | str = "",
                 timeout_seconds: float = 3.0) -> None:
        self._http = http_client
        self._key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self.timeout_seconds = timeout_seconds
        self.calls = 0

    def __repr__(self) -> str:
        return f"TavilyMarketPriceProvider(timeout_seconds={self.timeout_seconds})"

    async def lookup(self, product_name: str, country: str = "GB",
                     currency: str = "GBP") -> ReferencePriceRecord | None:
        if country != "GB" or currency != "GBP":
            return None
        self.calls += 1
        body = {
            "query": f"{product_name} price new UK",
            "search_depth": "fast",
            "max_results": 5,
            "topic": "general",
            "country": "united kingdom",
            "include_answer": False,
        }
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        async with asyncio.timeout(self.timeout_seconds):
            response = await self._http.post(TAVILY_URL, json=body, headers=headers,
                                             timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(f"tavily HTTP {response.status_code}",
                                        request=response.request, response=response)
        results = response.json().get("results") or []
        if not isinstance(results, list):
            return None
        return parse_reference(product_name, results)

    async def peek(self, product_name: str, country: str = "GB",
                   currency: str = "GBP") -> ReferencePriceRecord | None:
        return None
