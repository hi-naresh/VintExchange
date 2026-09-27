"""Refresh cached Tavily reference prices without touching rehearsed inventory.

Usage: python -m scripts.refresh_prices ["Sony WH-1000XM5" ...]
Requires MARKET_MODE=tavily and TAVILY_API_KEY. With --seed-empty-inventory it
may set initial list prices, but only when the inventory table is empty (it
never changes prices the demo has been rehearsed against).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from app.api.dependencies import build_market, build_repository
from app.config import Settings
from app.domain.models import format_pence

DEFAULT_PRODUCTS = ["noise cancelling headphones", "Sony WH-1000XM5"]


async def refresh(products: list[str], seed_empty_inventory: bool) -> int:
    settings = Settings()
    if settings.market_mode != "tavily":
        print("MARKET_MODE is not 'tavily'; nothing to refresh.")
        return 1
    async with httpx.AsyncClient(timeout=10) as http:
        repository = await build_repository(settings, http)
        market = build_market(settings, repository, http)
        for product in products:
            reference = await market.lookup(product)
            if reference is None:
                print(f"{product}: no reference")
                continue
            print(f"{product}: {format_pence(reference.price_pence)} "
                  f"({reference.source.value}, {reference.confidence.value} confidence, "
                  f"{len(reference.source_urls)} sources)")
        if seed_empty_inventory and not await repository.list_inventory():
            print("Inventory is empty; seed it with `python -m scripts.reset` first.")
        await repository.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("products", nargs="*", default=DEFAULT_PRODUCTS)
    parser.add_argument("--seed-empty-inventory", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(refresh(args.products, args.seed_empty_inventory))


if __name__ == "__main__":
    sys.exit(main())
