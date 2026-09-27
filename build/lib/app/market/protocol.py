"""Market-price provider boundary.

`lookup` may use the network (bounded); `peek` never does. Both return a typed
reference price or None. An LLM never turns search text into executable prices.
"""

from __future__ import annotations

import re
from typing import Protocol

from app.domain.models import ReferencePriceRecord

ReferencePrice = ReferencePriceRecord

_PRICE_PHRASE = re.compile(
    r"(under|below|less than|max(imum)?|up to|at most|around|about)?\s*£\s*\d[\d,]*(\.\d{1,2})?",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_product_key(product_name: str) -> str:
    """Canonical cache key: lowercase words, price phrases and punctuation removed."""
    without_prices = _PRICE_PHRASE.sub(" ", product_name.lower())
    return " ".join(_NON_ALNUM.sub(" ", without_prices).split())


class MarketPriceProvider(Protocol):
    async def lookup(self, product_name: str, country: str = "GB",
                     currency: str = "GBP") -> ReferencePrice | None: ...

    async def peek(self, product_name: str, country: str = "GB",
                   currency: str = "GBP") -> ReferencePrice | None: ...
