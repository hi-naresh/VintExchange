"""Shopify catalogue/draft orders and PostHog forwarding, with mocked HTTP."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.analytics.posthog import PostHogSink
from app.commerce.catalog import load_catalog
from app.commerce.shopify import (
    ShopifyCatalogClient,
    ShopifyDraftOrderSink,
    ShopifyError,
    map_products,
)
from app.config import ConfigurationError, Settings
from app.domain.models import CheckoutCommand, QuoteCreate, QuoteStatus
from app.repositories.seed import DEMO_MANDATE_ID
from app.repositories.sqlite import SQLiteRepository
from app.services.commerce import CommerceService
from app.services.events import EventRecorder
from tests.conftest import headphone_rfq


def product(handle, vendor, ptype, sku, price, qty, tags, currency="GBP"):
    return {"handle": handle, "title": handle.replace("-", " ").title(), "vendor": vendor,
            "productType": ptype, "tags": tags,
            "variants": {"nodes": [{"id": f"gid://shopify/ProductVariant/{sku}", "sku": sku,
                                    "availableForSale": qty > 0, "quantityAvailable": qty,
                                    "price": {"amount": price, "currencyCode": currency}}]}}


PRODUCTS = [
    product("bassline-pro", "Bassline Supply", "Audio", "BSL-PRO", "162.00", 5,
            ["merchant:bassline", "floor:148.00", "rating:4.4", "delivery:1"]),
    product("circuit-q45", "Circuit Electronics", "Audio", "CIR-Q45", "158.0", 5,
            ["merchant:circuit"]),
    product("usd-thing", "Elsewhere", "Audio", "USD-1", "10.00", 5, [], currency="USD"),
]


def shopify_settings(**extra) -> Settings:
    return Settings(_env_file=None, catalog_source="shopify",
                    shopify_store_domain="gx-demo.myshopify.com",
                    shopify_storefront_token=SecretStr("storefront-secret"), **extra)


def test_products_map_to_merchants_inventory_and_private_floors():
    catalogue = map_products(PRODUCTS, default_floor_ratio=0.9)
    assert [m["id"] for m in catalogue.merchants] == ["merchant-bassline", "merchant-circuit"]
    bassline, circuit = catalogue.inventory
    assert bassline["id"] == "inventory-bassline-pro"
    assert (bassline["list_price_pence"], bassline["floor_price_pence"]) == (16_200, 14_800)
    assert bassline["category"] == "audio" and bassline["delivery_days"] == 1
    assert bassline["external_id"] == "gid://shopify/ProductVariant/BSL-PRO"
    assert circuit["floor_price_pence"] == 14_220  # 90% default when no floor tag
    assert catalogue.skipped == ["usd-thing: price not in GBP"]


def test_shopify_catalogue_requires_credentials():
    with pytest.raises(ConfigurationError, match="SHOPIFY_STOREFRONT_TOKEN"):
        Settings(_env_file=None, catalog_source="shopify", shopify_store_domain="x")


async def test_storefront_client_paginates_and_loads_repository(tmp_path):
    pages = iter([
        {"data": {"products": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                               "nodes": PRODUCTS[:1]}}},
        {"data": {"products": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                               "nodes": PRODUCTS[1:]}}},
    ])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["X-Shopify-Storefront-Access-Token"],
                     json.loads(request.content)["variables"]))
        return httpx.Response(200, json=next(pages))

    repo = await SQLiteRepository.connect(tmp_path / "shop.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        description = await load_catalog(repo, shopify_settings(), http)
    assert seen[0][0] == "https://gx-demo.myshopify.com/api/2025-07/graphql.json"
    assert seen[0][1] == "storefront-secret" and seen[1][2] == {"cursor": "c1"}
    assert "2 merchants, 2 SKUs, skipped 1" in description
    mandate = await repo.get_active_mandate()
    assert mandate.allowed_merchant_ids == ["merchant-bassline", "merchant-circuit"]
    item = await repo.get_inventory_item("inventory-bassline-pro")
    assert item.external_price_pence == 16_200


async def test_storefront_errors_are_sanitized():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token storefront-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ShopifyError) as error:
            await ShopifyCatalogClient(http, shopify_settings()).fetch()
    assert "storefront-secret" not in str(error.value)


async def test_paid_fill_is_mirrored_as_discounted_draft_order(tmp_path):
    repo = await SQLiteRepository.connect(tmp_path / "shop.db")
    catalogue = map_products(PRODUCTS, 0.9)
    from app.repositories.seed import DEMO_MANDATE

    await repo.load_seed(catalogue.merchants, catalogue.inventory, DEMO_MANDATE_ID,
                         DEMO_MANDATE.model_copy(update={
                             "allowed_merchant_ids": ["merchant-bassline"]}))
    request = await repo.create_request(headphone_rfq(max_pence=15_000),
                                        mandate_id=DEMO_MANDATE_ID)
    quote = await repo.create_quote(
        QuoteCreate(request_id=request.id, merchant_id="merchant-bassline",
                    inventory_id="inventory-bassline-pro", price_pence=14_800,
                    delivery_days=1), status=QuoteStatus.VALID, rejection_reason=None)
    sent = {}

    def handler(r: httpx.Request) -> httpx.Response:
        sent["url"] = str(r.url)
        sent["token"] = r.headers["X-Shopify-Access-Token"]
        sent["input"] = json.loads(r.content)["variables"]["input"]
        return httpx.Response(200, json={"data": {"draftOrderCreate": {
            "draftOrder": {"id": "gid://shopify/DraftOrder/1", "name": "#D1",
                           "invoiceUrl": "https://x"}, "userErrors": []}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        settings = shopify_settings(shopify_admin_token=SecretStr("admin-secret"))
        commerce = CommerceService(repo, EventRecorder(repo),
                                   order_sink=ShopifyDraftOrderSink(http, settings))
        result = await commerce.checkout(CheckoutCommand(
            request_id=request.id, quote_id=quote.id, idempotency_key="shop-1"))
        await commerce.drain()

    assert result.accepted
    assert sent["url"].endswith("/admin/api/2025-07/graphql.json")
    assert sent["token"] == "admin-secret"
    [line] = sent["input"]["lineItems"]
    assert line["variantId"] == "gid://shopify/ProductVariant/BSL-PRO"
    assert line["appliedDiscount"]["value"] == 14.0
    assert "simulated-payment" in sent["input"]["tags"]
    events = [e.type for e in await repo.list_events(request.id)]
    assert "shopify_draft_order" in events


async def test_shopify_failure_never_undoes_the_fill(tmp_path, repository):
    request = await repository.create_request(headphone_rfq(), mandate_id=DEMO_MANDATE_ID)
    quote = await repository.create_quote(
        QuoteCreate(request_id=request.id, merchant_id="merchant-circuit",
                    inventory_id="inventory-circuit-q45", price_pence=15_800,
                    delivery_days=3), status=QuoteStatus.VALID, rejection_reason=None)

    class BrokenSink:
        async def create_draft_order(self, order, item):
            raise ShopifyError("admin HTTP 503")

    commerce = CommerceService(repository, EventRecorder(repository), order_sink=BrokenSink())
    result = await commerce.checkout(CheckoutCommand(
        request_id=request.id, quote_id=quote.id, idempotency_key="shop-2"))
    await commerce.drain()
    assert result.accepted and result.order.status == "paid"
    events = await repository.list_events(request.id)
    failed = next(e for e in events if e.type == "shopify_draft_order_failed")
    assert failed.payload["error"] == "admin HTTP 503"


async def test_events_are_forwarded_to_posthog(repository):
    captured = []

    def handler(r: httpx.Request) -> httpx.Response:
        captured.append((str(r.url), json.loads(r.content)))
        return httpx.Response(200, json={"status": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sink = PostHogSink(http, "phc_test", "https://eu.i.posthog.com")
        recorder = EventRecorder(repository, [sink])
        await recorder.emit("policy_rejected", stage="policy", request_id="req-1",
                            code="PER_ORDER_CAP_EXCEEDED", api_key="never-sent")
        await recorder.drain()
    [(url, body)] = captured
    assert url == "https://eu.i.posthog.com/i/v0/e/"
    assert body["event"] == "gx_policy_rejected" and body["distinct_id"] == "req-1"
    assert body["properties"]["code"] == "PER_ORDER_CAP_EXCEEDED"
    assert "never-sent" not in json.dumps(body["properties"])


async def test_posthog_outage_does_not_break_events(repository):
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        recorder = EventRecorder(repository, [PostHogSink(http, "k", "https://x.example")])
        event = await recorder.emit("request_created", request_id="req-2")
        await recorder.drain()
    assert event.type == "request_created"
