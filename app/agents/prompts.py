"""Deterministic prompt construction shared by the Grok and replay clients.

Prompts carry only the data an agent needs to propose. They are hashed for the
replay cache and are never written to events.
"""

from __future__ import annotations

import json

from app.domain.models import InventoryRecord, MerchantRecord, QuoteRecord, RequestRecord

RFQ_SYSTEM = (
    "You are a buyer's shopping agent. Convert the user's instruction into a request for "
    "quotes. Reply with ONLY a JSON object: {\"query\": str, \"category\": str, "
    "\"max_price_pence\": int, \"quantity\": int, \"deadline\": null}. "
    "Money is integer pence (GBP) and max_price_pence is the TOTAL for all units. Known "
    "categories: denim-jackets, leather-jackets, band-tees, wool-coats, audio, premium-audio, "
    "earbuds, turntables. You cannot change the buyer's mandate; just describe the request."
)

QUOTE_SYSTEM = (
    "You are a merchant's sales agent. Quote one SKU from your catalogue for the request. "
    "Reply with ONLY a JSON object: {\"sku\": str, \"price_pence\": int, "
    "\"delivery_days\": int, \"note\": str or null}. Money is integer pence. Never quote a "
    "SKU you do not stock. If the buyer countered, you may lower your price but not below "
    "your floor."
)

COUNTER_SYSTEM = (
    "You are a buyer's negotiating agent. Given the best current quote and the request's "
    "maximum, reply with ONLY a JSON object: {\"action\": \"counter\"|\"accept\"|\"walk\", "
    "\"price_pence\": int or null}. Counter at or below the request maximum."
)


def _dump(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def rfq_messages(text: str) -> tuple[str, str]:
    # No clock in the prompt: replay hashes must be stable across days. Missing
    # deadlines are defaulted server-side.
    return RFQ_SYSTEM, f"Instruction: {text}"


def quote_messages(merchant: MerchantRecord, inventory: list[InventoryRecord],
                   request: RequestRecord, round: int,
                   counter_pence: int | None) -> tuple[str, str]:
    catalogue = [
        {"sku": i.sku, "title": i.title, "list_price_pence": i.list_price_pence,
         "floor_price_pence": i.floor_price_pence,
         "delivery_days": i.delivery_days}
        for i in inventory
    ]
    payload = {
        "merchant": {"id": merchant.id, "name": merchant.name},
        "catalogue": catalogue,
        "request": {"query": request.query, "category": request.category,
                    "quantity": request.quantity},
        "round": round,
        "buyer_counter_pence": counter_pence,
    }
    return QUOTE_SYSTEM, _dump(payload)


def counter_messages(request: RequestRecord, quote: QuoteRecord, round: int) -> tuple[str, str]:
    payload = {
        "request": {"query": request.query, "max_price_pence": request.max_price_pence,
                    "quantity": request.quantity},
        "best_quote": {"merchant_id": quote.merchant_id, "price_pence": quote.price_pence,
                       "delivery_days": quote.delivery_days},
        "round": round,
    }
    return COUNTER_SYSTEM, _dump(payload)


def prompt_text(messages: tuple[str, str]) -> str:
    return f"{messages[0]}\n{messages[1]}"
