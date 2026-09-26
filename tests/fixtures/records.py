"""Small record factories for unit tests."""

from __future__ import annotations

from app.domain.models import InventoryRecord, MerchantRecord, QuoteRecord, RequestRecord

NOW = "2026-09-26T12:00:00.000000Z"


def merchant(merchant_id: str = "merchant-bassline") -> MerchantRecord:
    return MerchantRecord(id=merchant_id, name="Bassline Supply", rating=4.4, created_at=NOW)


def inventory() -> list[InventoryRecord]:
    return [InventoryRecord(
        id="inventory-bassline-pro", sku="BSL-PRO", merchant_id="merchant-bassline",
        title="Bassline Pro Wireless Headphones", category="audio", list_price_pence=16_200,
        floor_price_pence=14_800, stock=5, delivery_days=1)]


def request(max_price_pence: int = 15_000) -> RequestRecord:
    return RequestRecord(
        id="request-1", mandate_id="mandate-demo", query="noise cancelling headphones",
        category="audio", max_price_pence=max_price_pence, quantity=1, deadline=NOW,
        status="requested", round=1, created_at=NOW, updated_at=NOW)


def quote(price_pence: int = 16_200) -> QuoteRecord:
    return QuoteRecord(
        id="quote-1", request_id="request-1", merchant_id="merchant-bassline",
        inventory_id="inventory-bassline-pro", price_pence=price_pence, delivery_days=1,
        round=1, status="valid", rejection_reason=None, created_at=NOW)
