"""Connected-profile artifacts verified without credentials."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import ReserveCommand
from app.domain.policy import evaluate_quote
from app.repositories.protocol import Repository
from app.repositories.seed import DEMO_MANDATE, DEMO_MANDATE_ID, INVENTORY, MERCHANTS
from app.repositories.supabase import SupabaseRepository

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "supabase/migrations/001_schema.sql").read_text()
REALTIME = (ROOT / "supabase/migrations/002_realtime.sql").read_text()
SEED = (ROOT / "supabase/seed.sql").read_text()
TABLES = ["mandates", "merchants", "inventory", "requests", "quotes", "orders", "events",
          "exec_reports", "reference_prices"]


def test_supabase_repository_implements_every_protocol_method():
    required = {
        name for name, value in Repository.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert required <= set(dir(SupabaseRepository))


def test_sql_contains_database_guards():
    sql = SCHEMA
    assert "stock >= p_quantity" in sql
    assert "spent_pence + p_total <= budget_pence" in sql
    assert "unique (idempotency_key)" in sql.lower()
    assert "for update" in sql.lower()
    assert sql.lower().count("security invoker") >= 5


def test_every_table_exists_with_select_only_browser_access():
    for table in TABLES:
        assert f"create table if not exists public.{table} " in SCHEMA
    assert "for select to anon, authenticated" in SCHEMA
    assert not re.search(r"for (insert|update|delete|all) to anon", SCHEMA)
    for fn in ("reserve_atomic", "checkout_atomic", "release_failed_payment", "reset_demo",
               "set_active_mandate"):
        assert re.search(rf"revoke execute on function public\.{fn}\(.*\)\s+from public, anon, "
                         rf"authenticated", SCHEMA), fn
        assert re.search(rf"grant execute on function public\.{fn}\(.*\)\s+to service_role",
                         SCHEMA), fn


def test_realtime_publication_covers_dashboard_tables():
    for table in ("events", "quotes", "orders"):
        assert f"'{table}'" in REALTIME
    assert "supabase_realtime" in REALTIME


def test_seed_sql_matches_python_seed():
    for merchant in MERCHANTS:
        assert f"'{merchant['id']}'" in SEED
    for item in INVENTORY:
        assert (f"'{item['id']}', '{item['sku']}', '{item['merchant_id']}'" in SEED)
        assert f"{item['list_price_pence']}, {item['floor_price_pence']}, {item['stock']}" in SEED
    assert f"'{DEMO_MANDATE_ID}', {DEMO_MANDATE.budget_pence}" in SEED


def connected_settings(key: str = "sb_secret_TEST") -> Settings:
    return Settings(_env_file=None, profile="connected", supabase_url="https://x.supabase.co",
                    supabase_secret_key=SecretStr(key), supabase_publishable_key="sb_publishable",
                    grok_api_key=SecretStr("xai"))


def row(**values):
    base = {"created_at": "2026-09-26T12:00:00+00:00", "updated_at": "2026-09-26T12:00:00+00:00"}
    return {**base, **values}


async def test_reserve_uses_one_rpc_and_no_independent_writes():
    calls: list[tuple[str, str]] = []
    tables = {
        "orders": [],
        "requests": [row(id="req-1", mandate_id="mandate-demo", query="headphones",
                         category="audio", max_price_pence=17_000, quantity=1,
                         deadline="2026-09-27T12:00:00+00:00", status="resting", round=1)],
        "quotes": [row(id="quote-1", request_id="req-1", merchant_id="merchant-bassline",
                       inventory_id="inventory-bassline-pro", price_pence=16_200,
                       delivery_days=1, round=1, status="valid", origin="agent",
                       rejection_reason=None)],
        "mandates": [row(id="mandate-demo", budget_pence=30_000, spent_pence=0,
                         max_per_order_pence=20_000,
                         allowed_merchant_ids=["merchant-bassline"], orders_per_minute=5,
                         killed=False, active=True)],
        "inventory": [dict(INVENTORY[1])],
    }
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/rest/v1/")
        calls.append((request.method, path))
        seen_headers.update(request.headers)
        if path == "rpc/reserve_atomic":
            args = json.loads(request.content)
            assert args["p_total"] == 16_200 and args["p_commit_budget"] is True
            order = row(id=args["p_order_id"], request_id="req-1", quote_id="quote-1",
                        mandate_id="mandate-demo", inventory_id="inventory-bassline-pro",
                        idempotency_key=args["p_idempotency_key"], quantity=1,
                        price_pence=16_200, budget_committed=True, payment_reference=None,
                        status="reserved")
            return httpx.Response(200, json={"accepted": True, "code": "ALLOWED",
                                             "replayed": False, "order": order})
        return httpx.Response(200, json=tables.get(path, []))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        repo = SupabaseRepository(http, connected_settings())
        result = await repo.reserve_atomic(
            ReserveCommand(request_id="req-1", quote_id="quote-1", idempotency_key="k"),
            commit_budget=True, reference=None, check=evaluate_quote)

    assert result.accepted and result.order.status == "reserved"
    writes = [c for c in calls if c[0] != "GET"]
    assert writes == [("POST", "rpc/reserve_atomic")]
    assert seen_headers["apikey"] == "sb_secret_TEST"
    assert "authorization" not in seen_headers  # new-style secret keys use apikey only


async def test_policy_rejection_happens_before_any_rpc():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/rest/v1/")
        calls.append(path)
        data = {
            "requests": [row(id="req-1", mandate_id="m", query="q", category="audio",
                             max_price_pence=17_000, quantity=1,
                             deadline="2026-09-27T12:00:00+00:00", status="resting", round=1)],
            "quotes": [row(id="quote-1", request_id="req-1", merchant_id="merchant-bassline",
                           inventory_id="inventory-bassline-pro", price_pence=16_200,
                           delivery_days=1, round=1, status="valid", origin="agent",
                           rejection_reason=None)],
            "mandates": [row(id="m", budget_pence=30_000, spent_pence=0,
                             max_per_order_pence=20_000,
                             allowed_merchant_ids=["merchant-bassline"],
                             orders_per_minute=5, killed=True, active=True)],
            "inventory": [dict(INVENTORY[1])],
        }.get(path, [])
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await SupabaseRepository(http, connected_settings()).reserve_atomic(
            ReserveCommand(request_id="req-1", quote_id="quote-1", idempotency_key="k"),
            commit_budget=True, reference=None, check=evaluate_quote)
    assert result.decision.code == "KILL_SWITCH_ON"
    assert not any(c.startswith("rpc/") for c in calls)
