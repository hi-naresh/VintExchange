"""Restore deterministic seed state.

Offline: deletes and recreates only the configured SQLite demo database, and only
when it is a `.db` file under the project's `data/` directory (or a path passed
explicitly with --database, as tests do). Connected: calls the Supabase
`reset_demo()` RPC, but only with --connected so it is never triggered by accident.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.commerce.catalog import load_catalog
from app.config import PROJECT_ROOT, Settings
from app.repositories.seed import seed_repository
from app.repositories.sqlite import SQLiteRepository

DATA_DIR = PROJECT_ROOT / "data"


def validate_database_path(path: Path, *, explicit: bool) -> Path:
    resolved = path.resolve()
    if resolved.suffix != ".db":
        raise SystemExit(f"refusing to reset {resolved}: not a .db file")
    if not explicit and DATA_DIR.resolve() not in resolved.parents:
        raise SystemExit(f"refusing to reset {resolved}: not under {DATA_DIR}")
    return resolved


async def reset_sqlite(path: Path, settings: Settings | None = None) -> str:
    """Recreate the database and load the catalogue (seed by default)."""
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists():
            candidate.unlink()
    repository = await SQLiteRepository.connect(path)
    try:
        if settings is None:
            await seed_repository(repository)
            return "seed catalogue"
        return await load_catalog(repository, settings)
    finally:
        await repository.close()


async def reset_connected(settings: Settings) -> None:
    import httpx

    from app.repositories.supabase import SupabaseRepository

    async with httpx.AsyncClient(timeout=15) as http:
        await SupabaseRepository(http, settings).reset()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset Trading Agentic Commerce demo state.")
    parser.add_argument("--database", type=Path, help="explicit SQLite .db path")
    parser.add_argument("--catalog", choices=["seed", "shopify"],
                        help="override CATALOG_SOURCE for this reset")
    parser.add_argument("--connected", action="store_true",
                        help="reset the Supabase database via reset_demo()")
    args = parser.parse_args(argv)

    if args.connected:
        settings = Settings()
        if settings.profile != "connected":
            raise SystemExit("--connected requires PROFILE=connected")
        asyncio.run(reset_connected(settings))
        print("Supabase demo state reset.")
        return 0

    overrides = {"catalog_source": args.catalog} if args.catalog else {}
    settings = Settings(**overrides)
    if args.database is not None:
        path = validate_database_path(args.database, explicit=True)
    else:
        path = validate_database_path(settings.sqlite_path, explicit=False)
    description = asyncio.run(reset_sqlite(path, settings))
    print(f"Seeded {path} with {description}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
