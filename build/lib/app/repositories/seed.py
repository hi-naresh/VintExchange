"""Deterministic demo seed shared by SQLite reset and the Supabase seed.sql.

Headline story: the three audio SKUs quote £166, £162 and £158 (average £162).
Bassline's floor is £148, so the flash sale can reprice to exactly £148 and fill a
resting £150 limit request with a £14 saving against the average quote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.models import MandateCreate

if TYPE_CHECKING:
    from app.repositories.protocol import Repository

DEMO_MANDATE_ID = "mandate-demo"

MERCHANTS: list[dict[str, object]] = [
    {"id": "merchant-aurora", "name": "Aurora Audio", "rating": 4.6},
    {"id": "merchant-bassline", "name": "Bassline Supply", "rating": 4.4},
    {"id": "merchant-circuit", "name": "Circuit Electronics", "rating": 4.2},
]

INVENTORY: list[dict[str, object]] = [
    {"id": "inventory-aurora-studio", "sku": "AUR-STUDIO", "merchant_id": "merchant-aurora",
     "title": "Aurora Studio ANC Headphones", "category": "audio",
     "list_price_pence": 16_600, "floor_price_pence": 15_500, "stock": 5, "delivery_days": 2},
    {"id": "inventory-bassline-pro", "sku": "BSL-PRO", "merchant_id": "merchant-bassline",
     "title": "Bassline Pro Wireless Headphones", "category": "audio",
     "list_price_pence": 16_200, "floor_price_pence": 14_800, "stock": 5, "delivery_days": 1},
    {"id": "inventory-circuit-q45", "sku": "CIR-Q45", "merchant_id": "merchant-circuit",
     "title": "Circuit Q45 Noise-Cancelling Headphones", "category": "audio",
     "list_price_pence": 15_800, "floor_price_pence": 15_200, "stock": 5, "delivery_days": 3},
    {"id": "inventory-aurora-reference", "sku": "AUR-REF", "merchant_id": "merchant-aurora",
     "title": "Aurora Reference Pro Headphones", "category": "premium-audio",
     "list_price_pence": 90_000, "floor_price_pence": 85_000, "stock": 2, "delivery_days": 2},
    # Race-proof SKU: one unit, separate category so it never touches the headline story.
    {"id": "inventory-circuit-buds-ltd", "sku": "CIR-BUDS-LTD", "merchant_id": "merchant-circuit",
     "title": "Circuit Buds Limited Edition", "category": "earbuds",
     "list_price_pence": 4_900, "floor_price_pence": 4_500, "stock": 1, "delivery_days": 1},
    # Out-of-stock scenario SKU.
    {"id": "inventory-bassline-deck", "sku": "BSL-DECK", "merchant_id": "merchant-bassline",
     "title": "Bassline Deck Turntable", "category": "turntables",
     "list_price_pence": 24_000, "floor_price_pence": 22_000, "stock": 0, "delivery_days": 4},
]

DEMO_MANDATE = MandateCreate(
    budget_pence=30_000,
    max_per_order_pence=20_000,
    allowed_merchant_ids=[str(m["id"]) for m in MERCHANTS],
    orders_per_minute=5,
)

# Deterministic web-reference fallbacks, keyed by normalized product key. These are
# demo data, always low confidence, and never block a fill on their own.
SEED_REFERENCES: dict[str, int] = {
    "vintage denim jackets": 1_890,
    "vintage leather jackets": 4_800,
    "sony wh 1000xm5": 15_900,
    "noise cancelling headphones": 15_900,
    "wireless headphones": 15_900,
}


# ---------------------------------------------------------------- Fleek theme
# Fleek is a B2B wholesale marketplace for secondhand clothing where buyers ask
# for bulk lots: an RFQ by nature. Prices are per piece.
#
# Headline story: "50 grade-A vintage denim jackets under £18 each". Suppliers
# quote £19.80, £19.20 and £18.60 per piece (average £19.20, i.e. £960 for 50),
# so the request rests. Rag House's floor is £17.60: its flash sale fills the lot
# at £880 (saving £80), or the store's "Sell at £18.00" fills it at £900.

FLEEK_MERCHANTS: list[dict[str, object]] = [
    {"id": "merchant-northern", "name": "Northern Vintage Wholesale", "rating": 4.6},
    {"id": "merchant-raghouse", "name": "Rag House London", "rating": 4.4},
    {"id": "merchant-retrograde", "name": "Retrograde Supply Co.", "rating": 4.2},
]

FLEEK_INVENTORY: list[dict[str, object]] = [
    {"id": "inventory-northern-denim", "sku": "NVW-DEN-A", "merchant_id": "merchant-northern",
     "title": "Grade-A vintage denim jackets, mixed brands", "category": "denim-jackets",
     "list_price_pence": 1_980, "floor_price_pence": 1_850, "stock": 300, "delivery_days": 3},
    {"id": "inventory-raghouse-denim", "sku": "RHL-DEN-A", "merchant_id": "merchant-raghouse",
     "title": "Grade-A 90s denim jackets", "category": "denim-jackets",
     "list_price_pence": 1_920, "floor_price_pence": 1_760, "stock": 200, "delivery_days": 2},
    {"id": "inventory-retrograde-denim", "sku": "RSC-DEN-A", "merchant_id": "merchant-retrograde",
     "title": "Grade-A washed denim jackets", "category": "denim-jackets",
     "list_price_pence": 1_860, "floor_price_pence": 1_800, "stock": 250, "delivery_days": 4},
    {"id": "inventory-northern-leather", "sku": "NVW-LEA-A", "merchant_id": "merchant-northern",
     "title": "Grade-A vintage leather jackets", "category": "leather-jackets",
     "list_price_pence": 4_500, "floor_price_pence": 4_000, "stock": 120, "delivery_days": 3},
    # Race-proof lot: one bale only.
    {"id": "inventory-retrograde-tees", "sku": "RSC-TEE-BALE", "merchant_id": "merchant-retrograde",
     "title": "Single-stitch band tees, one 100-piece bale", "category": "band-tees",
     "list_price_pence": 90_000, "floor_price_pence": 85_000, "stock": 1, "delivery_days": 2},
    # Out-of-stock scenario.
    {"id": "inventory-raghouse-wool", "sku": "RHL-WOOL", "merchant_id": "merchant-raghouse",
     "title": "Wool overcoats", "category": "wool-coats",
     "list_price_pence": 3_500, "floor_price_pence": 3_200, "stock": 0, "delivery_days": 5},
]

FLEEK_MANDATE = MandateCreate(
    budget_pence=200_000,
    max_per_order_pence=120_000,
    allowed_merchant_ids=[str(m["id"]) for m in FLEEK_MERCHANTS],
    orders_per_minute=5,
)

THEMES: dict[str, tuple[list[dict[str, object]], list[dict[str, object]], MandateCreate]] = {
    "electronics": (MERCHANTS, INVENTORY, DEMO_MANDATE),
    "fleek": (FLEEK_MERCHANTS, FLEEK_INVENTORY, FLEEK_MANDATE),
}

# One-click flash sale per theme: (merchant, inventory, price per piece).
FLASH_SALES: dict[str, dict[str, object]] = {
    "electronics": {"merchant_id": "merchant-bassline", "inventory_id": "inventory-bassline-pro",
                    "price_pence": 14_800, "label": "Bassline flash sale: £148"},
    "fleek": {"merchant_id": "merchant-raghouse", "inventory_id": "inventory-raghouse-denim",
              "price_pence": 1_760, "label": "Rag House flash sale: £17.60 a piece"},
}


async def seed_repository(repository: Repository, theme: str = "electronics") -> None:
    merchants, inventory, mandate = THEMES[theme]
    await repository.load_seed(merchants, inventory, DEMO_MANDATE_ID, mandate)
