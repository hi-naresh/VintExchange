"""Shopify integration: real catalogue in, draft orders out.

Catalogue (Storefront API, read-only token)
- Each product's `vendor` is a merchant; `productType` is the category.
- First variant supplies SKU, price (GBP) and stock (`quantityAvailable`, which
  needs the unauthenticated_read_product_inventory scope; otherwise
  availableForSale maps to a default stock).
- Merchant floor prices are private merchant data, so they come from product
  tags, never from the agent: `floor:148.00`. Optional tags: `merchant:bassline`
  (stable merchant id), `rating:4.4`, `delivery:1`.
- Inventory ids are `inventory-<handle>`, so demo actions stay stable.

Orders (Admin API, optional token with write_draft_orders)
- When our own transaction marks an order paid, a draft order is created in the
  merchant's Shopify admin with the negotiated discount applied. The draft is a
  mirror for the merchant: payment in this demo remains simulated, and a Shopify
  failure never undoes or blocks the fill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.config import Settings
from app.domain.models import InventoryRecord, OrderRecord, format_pence

DEFAULT_STOCK_WHEN_UNKNOWN = 10
DEFAULT_RATING = 4.5
DEFAULT_DELIVERY_DAYS = 2

PRODUCTS_QUERY = """
query Catalogue($cursor: String) {
  products(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      handle
      title
      vendor
      productType
      tags
      variants(first: 1) {
        nodes {
          id
          sku
          availableForSale
          quantityAvailable
          price { amount currencyCode }
        }
      }
    }
  }
}
"""

DRAFT_ORDER_MUTATION = """
mutation DraftOrder($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) {
    draftOrder { id name invoiceUrl }
    userErrors { field message }
  }
}
"""


class ShopifyError(RuntimeError):
    """Raised with a sanitized message; never includes tokens."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


def to_pence(amount: str | float | int) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _tag(tags: list[str], name: str) -> str | None:
    prefix = f"{name}:"
    for tag in tags:
        if tag.lower().startswith(prefix):
            return tag[len(prefix):].strip()
    return None


@dataclass
class Catalogue:
    merchants: list[dict[str, object]] = field(default_factory=list)
    inventory: list[dict[str, object]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def map_products(products: list[dict[str, Any]], default_floor_ratio: float) -> Catalogue:
    catalogue = Catalogue()
    merchants: dict[str, dict[str, object]] = {}
    for product in products:
        variants = (product.get("variants") or {}).get("nodes") or []
        if not variants:
            catalogue.skipped.append(f"{product.get('handle')}: no variants")
            continue
        variant = variants[0]
        price = variant.get("price") or {}
        if price.get("currencyCode") != "GBP":
            catalogue.skipped.append(f"{product.get('handle')}: price not in GBP")
            continue
        tags = [str(t) for t in product.get("tags") or []]
        vendor = str(product.get("vendor") or "Unknown merchant")
        merchant_id = f"merchant-{slug(_tag(tags, 'merchant') or vendor)}"
        if merchant_id not in merchants:
            rating = float(_tag(tags, "rating") or DEFAULT_RATING)
            merchants[merchant_id] = {"id": merchant_id, "name": vendor,
                                      "rating": max(0.0, min(5.0, rating))}
        list_pence = to_pence(price["amount"])
        floor_tag = _tag(tags, "floor")
        floor_pence = to_pence(floor_tag) if floor_tag else int(list_pence * default_floor_ratio)
        floor_pence = max(1, min(floor_pence, list_pence))
        quantity = variant.get("quantityAvailable")
        if quantity is None:
            quantity = DEFAULT_STOCK_WHEN_UNKNOWN if variant.get("availableForSale") else 0
        handle = str(product.get("handle") or slug(str(product.get("title"))))
        catalogue.inventory.append({
            "id": f"inventory-{slug(handle)}",
            "sku": str(variant.get("sku") or handle).upper(),
            "merchant_id": merchant_id,
            "title": str(product.get("title") or handle),
            "category": slug(str(product.get("productType") or "general")),
            "list_price_pence": list_pence,
            "floor_price_pence": floor_pence,
            "stock": max(0, int(quantity)),
            "delivery_days": int(_tag(tags, "delivery") or DEFAULT_DELIVERY_DAYS),
            "external_id": variant.get("id"),
            "external_price_pence": list_pence,
        })
    catalogue.merchants = list(merchants.values())
    return catalogue


class ShopifyCatalogClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        domain = settings.shopify_store_domain.removeprefix("https://").rstrip("/")
        self.domain = domain
        self._url = f"https://{domain}/api/{settings.shopify_api_version}/graphql.json"
        self._token = settings.shopify_storefront_token
        self._floor_ratio = settings.shopify_default_floor_ratio

    async def _query(self, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(
                self._url, json={"query": PRODUCTS_QUERY, "variables": variables},
                headers={"X-Shopify-Storefront-Access-Token": self._token.get_secret_value()},
                timeout=15,
            )
        except httpx.HTTPError as error:
            raise ShopifyError(f"storefront request failed: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise ShopifyError(f"storefront HTTP {response.status_code}")
        body = response.json()
        if body.get("errors"):
            messages = "; ".join(str(e.get("message")) for e in body["errors"])[:300]
            raise ShopifyError(f"storefront errors: {messages}")
        return body["data"]["products"]

    async def fetch(self) -> Catalogue:
        products: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):  # hard page cap
            page = await self._query({"cursor": cursor})
            products.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        return map_products(products, self._floor_ratio)


class ShopifyDraftOrderSink:
    """Mirror a paid (simulated) fill into Shopify as a draft order."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        domain = settings.shopify_store_domain.removeprefix("https://").rstrip("/")
        self._url = f"https://{domain}/admin/api/{settings.shopify_api_version}/graphql.json"
        self._token = settings.shopify_admin_token

    async def create_draft_order(self, order: OrderRecord,
                                 item: InventoryRecord) -> dict[str, str]:
        if not item.external_id:
            raise ShopifyError(f"{item.id} has no Shopify variant id")
        original = item.external_price_pence or item.list_price_pence
        discount_per_unit = max(0, original - order.price_pence)
        line: dict[str, Any] = {"variantId": item.external_id, "quantity": order.quantity}
        if discount_per_unit:
            line["appliedDiscount"] = {
                "value": float(Decimal(discount_per_unit) / 100),
                "valueType": "FIXED_AMOUNT",
                "title": "Negotiated by Vint Exchange",
            }
        payload = {
            "query": DRAFT_ORDER_MUTATION,
            "variables": {"input": {
                "lineItems": [line],
                "note": (f"Vint Exchange order {order.id}: agent-negotiated at "
                         f"{format_pence(order.price_pence)} per unit. Payment simulated."),
                "tags": ["trading-agentic-commerce", "agentic-commerce", "simulated-payment"],
            }},
        }
        try:
            response = await self._http.post(
                self._url, json=payload,
                headers={"X-Shopify-Access-Token": self._token.get_secret_value()}, timeout=10)
        except httpx.HTTPError as error:
            raise ShopifyError(f"admin request failed: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise ShopifyError(f"admin HTTP {response.status_code}")
        body = response.json()
        if body.get("errors"):
            raise ShopifyError("admin errors: " + str(body["errors"])[:300])
        result = body["data"]["draftOrderCreate"]
        if result["userErrors"]:
            raise ShopifyError("draft order rejected: " + "; ".join(
                e["message"] for e in result["userErrors"])[:300])
        draft = result["draftOrder"]
        return {"id": draft["id"], "name": draft["name"]}
