"""Scenario-driven deterministic LLM client for tests and the offline demo.

Responses are JSON *text* and pass through the same strict parser as the real
client, so malformed scripted output exercises the real rejection path.

Default (unscripted) behaviour tells the headline story:
- merchants quote their cheapest in-stock SKU at list price and hold on counter;
- the buyer counters once at the request maximum, then walks away;
- free text is parsed with simple deterministic rules.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from app.agents.protocol import (
    CounterProposal,
    QuoteProposal,
    RFQProposal,
    parse_model,
)
from app.domain.models import InventoryRecord, MerchantRecord, QuoteRecord, RequestRecord

ScriptEntry = str | Callable[..., str]
Script = dict[str, list[ScriptEntry] | Callable[..., str]]

_AMOUNT = re.compile(r"£\s*(\d[\d,]*)(?:\.(\d{2}))?")
# First whole number that is not a price ("50 grade-A jackets under £18 each" -> 50).
_QUANTITY = re.compile(r"(?<![£\d.,])(?<!£ )\b(\d{1,4})\b(?![.,]\d)")
_PER_UNIT = re.compile(r"\b(each|per piece|per item|per unit|apiece|a piece)\b", re.IGNORECASE)
_CATEGORY_WORDS = [
    ("leather-jackets", ("leather",)),
    ("wool-coats", ("wool", "overcoat", " coat")),
    ("band-tees", ("band tee", " tees", "t-shirt")),
    ("denim-jackets", ("denim", "jacket")),
    ("premium-audio", ("premium", "reference", "£900", "flagship")),
    ("earbuds", ("earbud", "buds")),
    ("turntables", ("turntable", "record player", "deck")),
    ("audio", ("headphone", "headset", "anc", "noise")),
]
_QUERIES = {
    "leather-jackets": "vintage leather jackets",
    "wool-coats": "wool overcoats",
    "band-tees": "single-stitch band tees",
    "denim-jackets": "grade-A vintage denim jackets",
    "premium-audio": "premium reference headphones",
    "earbuds": "limited edition earbuds",
    "turntables": "turntable",
    "audio": "noise cancelling headphones",
}


def _amounts(text: str) -> list[int]:
    return [int(p.replace(",", "")) * 100 + int(d or 0) for p, d in _AMOUNT.findall(text)]


def default_parse_rfq(text: str) -> str:
    lowered = f" {text.lower()} "
    amounts = _amounts(text)
    quantity_match = _QUANTITY.search(text)
    quantity = int(quantity_match.group(1)) if quantity_match else 1
    price = max(amounts) if amounts else 15_000
    max_total = price * quantity if _PER_UNIT.search(text) else price
    category = next((c for c, words in _CATEGORY_WORDS if any(w in lowered for w in words)),
                    None)
    if category is None:
        category = "premium-audio" if max_total >= 50_000 else "audio"
    return json.dumps({
        "query": _QUERIES[category],
        "category": category,
        "max_price_pence": max_total,
        "quantity": quantity,
        "deadline": None,
    })


def default_quote(merchant: MerchantRecord, inventory: list[InventoryRecord],
                  request: RequestRecord, round: int, counter_pence: int | None) -> str:
    candidates = sorted(inventory, key=lambda i: (i.list_price_pence, i.sku))
    item = candidates[0]
    return json.dumps({"sku": item.sku, "price_pence": item.list_price_pence,
                       "delivery_days": item.delivery_days,
                       "note": "Holding at list price." if counter_pence else None})


def default_counter(request: RequestRecord, quote: QuoteRecord, round: int) -> str:
    if round == 1:
        return json.dumps({"action": "counter", "price_pence": request.max_price_pence})
    return json.dumps({"action": "walk", "price_pence": None})


class FakeLLMClient:
    model_name = "fake-deterministic"

    def __init__(self, script: Script | None = None,
                 delays: dict[str, float | Callable[..., float]] | None = None) -> None:
        self.script: Script = dict(script or {})
        self.delays = dict(delays or {})
        self.calls: dict[str, int] = defaultdict(int)
        self._positions: dict[str, int] = defaultdict(int)

    @property
    def counter_calls(self) -> int:
        return self.calls["counter"]

    def _next(self, operation: str, default: Callable[..., str], **kwargs: Any) -> str:
        entry = self.script.get(operation)
        if entry is None:
            return default(**kwargs)
        if callable(entry):
            return entry(**kwargs)
        index = min(self._positions[operation], len(entry) - 1)
        self._positions[operation] += 1
        chosen = entry[index]
        return chosen(**kwargs) if callable(chosen) else chosen

    async def _delay(self, operation: str, **kwargs: Any) -> None:
        delay = self.delays.get(operation)
        if delay is None:
            return
        seconds = delay(**kwargs) if callable(delay) else delay
        if seconds:
            await asyncio.sleep(seconds)

    async def parse_rfq(self, text: str) -> RFQProposal:
        self.calls["parse_rfq"] += 1
        await self._delay("parse_rfq", text=text)
        return parse_model(RFQProposal, self._next("parse_rfq", default_parse_rfq, text=text),
                           "parse_rfq")

    async def quote(self, merchant: MerchantRecord, inventory: list[InventoryRecord],
                    request: RequestRecord, round: int,
                    counter_pence: int | None = None) -> QuoteProposal:
        self.calls["quote"] += 1
        kwargs = {"merchant": merchant, "inventory": inventory, "request": request,
                  "round": round, "counter_pence": counter_pence}
        await self._delay("quote", **kwargs)
        return parse_model(QuoteProposal, self._next("quote", default_quote, **kwargs), "quote")

    async def counter(self, request: RequestRecord, quote: QuoteRecord,
                      round: int) -> CounterProposal:
        self.calls["counter"] += 1
        kwargs = {"request": request, "quote": quote, "round": round}
        await self._delay("counter", **kwargs)
        return parse_model(CounterProposal, self._next("counter", default_counter, **kwargs),
                           "counter")
