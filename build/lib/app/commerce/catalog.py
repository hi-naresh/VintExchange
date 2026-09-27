"""Load the merchant catalogue from the configured source (seed or Shopify)."""

from __future__ import annotations

import httpx

from app.commerce.shopify import ShopifyCatalogClient
from app.config import Settings
from app.domain.models import MandateCreate
from app.repositories.protocol import Repository
from app.repositories.seed import DEMO_MANDATE, DEMO_MANDATE_ID, THEMES


async def load_catalog(repository: Repository, settings: Settings,
                       http: httpx.AsyncClient | None = None) -> str:
    """Seed merchants, inventory and the demo mandate. Returns a short description."""
    if settings.catalog_source == "seed":
        merchants, inventory, seed_mandate = THEMES[settings.demo_theme]
        await repository.load_seed(merchants, inventory, DEMO_MANDATE_ID, seed_mandate)
        return (f"{settings.demo_theme} seed catalogue: {len(merchants)} merchants, "
                f"{len(inventory)} SKUs")

    owns_client = http is None
    client = http or httpx.AsyncClient(timeout=15)
    try:
        catalogue = await ShopifyCatalogClient(client, settings).fetch()
    finally:
        if owns_client:
            await client.aclose()
    if not catalogue.merchants:
        raise RuntimeError("Shopify returned no GBP products; check the store and token")
    mandate = MandateCreate(
        budget_pence=DEMO_MANDATE.budget_pence,
        max_per_order_pence=DEMO_MANDATE.max_per_order_pence,
        allowed_merchant_ids=[str(m["id"]) for m in catalogue.merchants],
        orders_per_minute=DEMO_MANDATE.orders_per_minute,
    )
    await repository.load_seed(catalogue.merchants, catalogue.inventory, DEMO_MANDATE_ID,
                               mandate)
    skipped = f", skipped {len(catalogue.skipped)}" if catalogue.skipped else ""
    return (f"Shopify catalogue from {settings.shopify_store_domain}: "
            f"{len(catalogue.merchants)} merchants, {len(catalogue.inventory)} SKUs{skipped}")
