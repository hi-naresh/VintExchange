"""Supabase (PostgREST + RPC) repository for the connected profile.

Ordinary CRUD uses table REST endpoints. Every stock-, budget-, or
payment-moving operation is a single Postgres function call (`reserve_atomic`,
`checkout_atomic`, `release_failed_payment`, `set_active_mandate`) so guards run
at the database transaction boundary. The adapter never emulates a transaction
with several independent REST writes.

The server-side secret key is used only here; it is never sent to browsers.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from app.config import Settings
from app.domain.models import (
    OPEN_REQUEST_STATUSES,
    DashboardCounters,
    DashboardSnapshot,
    EventCreate,
    EventRecord,
    ExecReportRecord,
    InventoryRecord,
    MandateCreate,
    MandateRecord,
    MerchantRecord,
    OrderRecord,
    OrderStatus,
    PolicyCode,
    PolicyDecision,
    QuoteCreate,
    QuoteOrigin,
    QuoteRecord,
    QuoteStatus,
    ReferencePriceCreate,
    ReferencePriceRecord,
    RequestRecord,
    RequestStatus,
    ReservationResult,
    ReserveCommand,
    RFQCreate,
    iso,
    utcnow,
)
from app.domain.policy import PolicyContext
from app.repositories.protocol import (
    NotFound,
    PolicyCheck,
    ReportBuilder,
    RepositoryConflict,
    RepositoryError,
)
from app.repositories.sqlite import new_id, stage_latency


def _eq(value: str | int | bool) -> str:
    return f"eq.{str(value).lower() if isinstance(value, bool) else value}"


class SupabaseRepository:
    def __init__(self, http: httpx.AsyncClient, settings: Settings,
                 rest_url: str | None = None) -> None:
        self._http = http
        # rest_url lets tests point at a bare PostgREST server (no /rest/v1 gateway).
        self._rest = (rest_url or settings.supabase_url.rstrip("/") + "/rest/v1").rstrip("/")
        key = settings.supabase_secret_key.get_secret_value()
        self._headers = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_"):  # legacy JWT service_role keys also need Bearer
            self._headers["Authorization"] = f"Bearer {key}"

    def __repr__(self) -> str:
        return f"SupabaseRepository(rest={self._rest!r})"

    # ------------------------------------------------------------------ plumbing

    async def _request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                       json: Any = None, prefer: str | None = None) -> Any:
        headers = dict(self._headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            response = await self._http.request(method, f"{self._rest}/{path}", params=params,
                                                json=json, headers=headers)
        except httpx.HTTPError as error:
            raise RepositoryError(f"supabase {type(error).__name__}") from None
        if response.status_code == 409:
            raise RepositoryConflict(self._error_message(response))
        if response.status_code >= 400:
            message = self._error_message(response)
            if response.status_code == 404 or "P0002" in message:
                raise NotFound(path, message)
            if message.startswith("23"):  # integrity_constraint_violation class
                raise RepositoryConflict(message)
            raise RepositoryError(f"supabase HTTP {response.status_code}: {message}")
        if not response.content:
            return None
        return response.json()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        return f"{body.get('code', '')} {body.get('message', '')}".strip()[:200]

    async def _select(self, table: str, **filters: str) -> list[dict[str, Any]]:
        params = {"select": "*", **filters}
        return await self._request("GET", table, params=params) or []

    async def _one(self, table: str, **filters: str) -> dict[str, Any] | None:
        rows = await self._select(table, limit="1", **filters)
        return rows[0] if rows else None

    async def _insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self._request("POST", table, json=row, prefer="return=representation")
        return rows[0]

    async def _patch(self, table: str, values: dict[str, Any],
                     **filters: str) -> list[dict[str, Any]]:
        return await self._request("PATCH", table, params=filters, json=values,
                                   prefer="return=representation") or []

    async def _rpc(self, function: str, args: dict[str, Any]) -> Any:
        return await self._request("POST", f"rpc/{function}", json=args)

    # ----------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        await self._select("merchants", limit="1")

    async def reset(self) -> None:
        await self._rpc("reset_demo", {})

    async def close(self) -> None:
        return None

    async def load_seed(self, merchants: list[dict[str, object]],
                        inventory: list[dict[str, object]], mandate_id: str,
                        mandate: MandateCreate) -> None:
        # reset_demo() truncates runtime rows and re-runs supabase/seed.sql's seed_demo().
        await self._rpc("reset_demo", {})

    # ------------------------------------------------------------------- mandate

    async def set_mandate(self, command: MandateCreate) -> MandateRecord:
        known = {m.id for m in await self.list_merchants()}
        unknown = set(command.allowed_merchant_ids) - known
        if unknown:
            raise NotFound("merchant", ", ".join(sorted(unknown)))
        row = await self._rpc("set_active_mandate", {
            "p_id": new_id("mandate"), "p_budget_pence": command.budget_pence,
            "p_max_per_order_pence": command.max_per_order_pence,
            "p_allowed_merchant_ids": command.allowed_merchant_ids,
            "p_orders_per_minute": command.orders_per_minute,
        })
        return MandateRecord.model_validate(row)

    async def get_active_mandate(self) -> MandateRecord | None:
        row = await self._one("mandates", active="eq.true")
        return MandateRecord.model_validate(row) if row else None

    async def set_killed(self, killed: bool) -> MandateRecord:
        rows = await self._patch("mandates", {"killed": killed, "updated_at": iso(utcnow())},
                                 active="eq.true")
        if not rows:
            raise NotFound("mandate", "active")
        return MandateRecord.model_validate(rows[0])

    # ----------------------------------------------------------------- catalogue

    async def list_merchants(self) -> list[MerchantRecord]:
        return [MerchantRecord.model_validate(r)
                for r in await self._select("merchants", order="id")]

    async def list_inventory(self, category: str | None = None, *,
                             in_stock_only: bool = False) -> list[InventoryRecord]:
        filters = {"order": "id"}
        if category is not None:
            filters["category"] = _eq(category)
        if in_stock_only:
            filters["stock"] = "gt.0"
        return [InventoryRecord.model_validate(r)
                for r in await self._select("inventory", **filters)]

    async def get_inventory_item(self, inventory_id: str) -> InventoryRecord | None:
        row = await self._one("inventory", id=_eq(inventory_id))
        return InventoryRecord.model_validate(row) if row else None

    async def set_list_price(self, inventory_id: str, price_pence: int) -> InventoryRecord:
        rows = await self._patch("inventory", {"list_price_pence": price_pence},
                                 id=_eq(inventory_id), floor_price_pence=f"lte.{price_pence}")
        if not rows:
            raise RepositoryConflict("reprice rejected by floor guard")
        return InventoryRecord.model_validate(rows[0])

    async def add_stock(self, inventory_id: str, quantity: int) -> InventoryRecord:
        # Optimistic concurrency: only apply if stock is unchanged since the read.
        for _ in range(5):
            item = await self.get_inventory_item(inventory_id)
            if item is None:
                raise NotFound("inventory", inventory_id)
            rows = await self._patch("inventory", {"stock": item.stock + quantity},
                                     id=_eq(inventory_id), stock=f"eq.{item.stock}")
            if rows:
                return InventoryRecord.model_validate(rows[0])
        raise RepositoryConflict("restock contended; retry")

    # ------------------------------------------------------------------ requests

    async def create_request(self, command: RFQCreate, *, mandate_id: str,
                             agent: str | None = None) -> RequestRecord:
        row = await self._insert("requests", {
            "id": new_id("req"), "mandate_id": mandate_id, "query": command.query,
            "category": command.category, "max_price_pence": command.max_price_pence,
            "quantity": command.quantity, "deadline": iso(command.deadline),
            "status": RequestStatus.REQUESTED.value, "round": 0, "agent": agent,
        })
        return RequestRecord.model_validate(row)

    async def get_request(self, request_id: str) -> RequestRecord | None:
        row = await self._one("requests", id=_eq(request_id))
        return RequestRecord.model_validate(row) if row else None

    async def update_request(self, request_id: str, *, status: RequestStatus,
                             round: int | None = None) -> RequestRecord:
        values: dict[str, Any] = {"status": status.value, "updated_at": iso(utcnow())}
        if round is not None:
            values["round"] = round
        rows = await self._patch("requests", values, id=_eq(request_id),
                                 status="neq.cancelled")
        if rows:
            return RequestRecord.model_validate(rows[0])
        current = await self.get_request(request_id)
        if current is None:
            raise NotFound("request", request_id)
        return current

    async def amend_request(self, request_id: str, *, max_price_pence: int,
                            quantity: int) -> RequestRecord | None:
        rows = await self._patch(
            "requests", {"max_price_pence": max_price_pence, "quantity": quantity,
                         "updated_at": iso(utcnow())},
            id=_eq(request_id), status="in.(requested,quoted,negotiating,resting)")
        if rows:
            return RequestRecord.model_validate(rows[0])
        if await self.get_request(request_id) is None:
            raise NotFound("request", request_id)
        return None

    async def cancel_request(self, request_id: str) -> RequestRecord | None:
        rows = await self._patch(
            "requests", {"status": RequestStatus.CANCELLED.value, "updated_at": iso(utcnow())},
            id=_eq(request_id), status="in.(requested,quoted,negotiating,resting)")
        if rows:
            return RequestRecord.model_validate(rows[0])
        if await self.get_request(request_id) is None:
            raise NotFound("request", request_id)
        return None

    async def list_resting_requests(self, category: str) -> list[RequestRecord]:
        rows = await self._select("requests", status="eq.resting", category=_eq(category),
                                  deadline=f"gt.{iso(utcnow())}", order="created_at.asc")
        return [RequestRecord.model_validate(r) for r in rows]

    # -------------------------------------------------------------------- quotes

    async def create_quote(self, command: QuoteCreate, *, status: QuoteStatus,
                           rejection_reason: str | None,
                           origin: QuoteOrigin = QuoteOrigin.AGENT) -> QuoteRecord:
        row = await self._insert("quotes", {
            "id": new_id("quote"), **command.model_dump(), "status": status.value,
            "origin": origin.value, "rejection_reason": rejection_reason,
        })
        return QuoteRecord.model_validate(row)

    async def record_failed_quote(self, *, request_id: str, merchant_id: str, round: int,
                                  reason: str) -> QuoteRecord:
        row = await self._insert("quotes", {
            "id": new_id("quote"), "request_id": request_id, "merchant_id": merchant_id,
            "round": round, "status": QuoteStatus.REJECTED.value, "origin": "agent",
            "rejection_reason": reason,
        })
        return QuoteRecord.model_validate(row)

    async def get_quote(self, quote_id: str) -> QuoteRecord | None:
        row = await self._one("quotes", id=_eq(quote_id))
        return QuoteRecord.model_validate(row) if row else None

    async def set_quote_status(self, quote_id: str, status: QuoteStatus) -> QuoteRecord:
        rows = await self._patch("quotes", {"status": status.value}, id=_eq(quote_id))
        if not rows:
            raise NotFound("quote", quote_id)
        return QuoteRecord.model_validate(rows[0])

    async def list_quotes(self, request_id: str) -> list[QuoteRecord]:
        rows = await self._select("quotes", request_id=_eq(request_id), order="created_at.asc")
        return [QuoteRecord.model_validate(r) for r in rows]

    # -------------------------------------------------------------------- events

    async def create_event(self, event: EventCreate) -> EventRecord:
        row = await self._insert("events", {"id": new_id("evt"), **event.model_dump(mode="json")})
        return EventRecord.model_validate(row)

    async def list_events(self, request_id: str | None = None,
                          limit: int = 200) -> list[EventRecord]:
        filters = {"order": "created_at.desc", "limit": str(limit)}
        if request_id is not None:
            filters["request_id"] = _eq(request_id)
        return [EventRecord.model_validate(r) for r in await self._select("events", **filters)]

    # ---------------------------------------------------------- reference prices

    async def get_reference_price(self, query_key: str, country: str = "GB",
                                  currency: str = "GBP") -> ReferencePriceRecord | None:
        row = await self._one("reference_prices", query_key=_eq(query_key),
                              country=_eq(country), currency=_eq(currency))
        return ReferencePriceRecord.model_validate(row) if row else None

    async def upsert_reference_price(self, record: ReferencePriceCreate) -> ReferencePriceRecord:
        rows = await self._request(
            "POST", "reference_prices", params={"on_conflict": "query_key,country,currency"},
            json={"id": new_id("ref"), **record.model_dump(mode="json")},
            prefer="resolution=merge-duplicates,return=representation",
        )
        return ReferencePriceRecord.model_validate(rows[0])

    # ------------------------------------------------------------ orders/reports

    async def get_order(self, order_id: str) -> OrderRecord | None:
        row = await self._one("orders", id=_eq(order_id))
        return OrderRecord.model_validate(row) if row else None

    async def get_order_by_idempotency_key(self, key: str) -> OrderRecord | None:
        row = await self._one("orders", idempotency_key=_eq(key))
        return OrderRecord.model_validate(row) if row else None

    async def count_orders(self) -> int:
        return len(await self._request("GET", "orders", params={"select": "id"}) or [])

    async def get_report(self, order_id: str) -> ExecReportRecord | None:
        row = await self._one("exec_reports", order_id=_eq(order_id))
        return ExecReportRecord.model_validate(row) if row else None

    # ------------------------------------------------------ atomic transactions

    async def _recent_orders(self, mandate_id: str, exclude: str | None = None) -> int:
        since = iso(utcnow() - timedelta(seconds=60))
        rows = await self._request("GET", "orders", params={
            "select": "id", "mandate_id": _eq(mandate_id), "created_at": f"gte.{since}",
            "status": "in.(reserved,paid)"}) or []
        return sum(1 for r in rows if r["id"] != exclude)

    async def reserve_atomic(self, command: ReserveCommand, *, commit_budget: bool,
                             reference: ReferencePriceRecord | None,
                             check: PolicyCheck) -> ReservationResult:
        # 1. Read current state and run the pure policy (application boundary).
        existing = await self.get_order_by_idempotency_key(command.idempotency_key)
        request = await self.get_request(command.request_id)
        quote = await self.get_quote(command.quote_id)
        if request is None:
            raise NotFound("request", command.request_id)
        if quote is None or quote.request_id != request.id:
            raise NotFound("quote", command.quote_id)
        if quote.status is QuoteStatus.REJECTED or quote.price_pence is None:
            raise RepositoryConflict(f"quote {quote.id} was rejected")
        decision = await self._precheck(existing, request, quote, commit_budget, reference,
                                        check)
        if not decision.allowed and existing is None:
            # A concurrent call with the same key may have just won: replay it instead.
            existing = await self.get_order_by_idempotency_key(command.idempotency_key)
            if existing is not None:
                decision = await self._precheck(existing, request, quote, commit_budget,
                                                reference, check)
        if not decision.allowed:
            return ReservationResult(accepted=False, decision=decision)

        # 2. One RPC re-applies kill/idempotency/stock/budget guards under row locks.
        result = await self._rpc("reserve_atomic", {
            "p_order_id": new_id("order"), "p_request_id": request.id, "p_quote_id": quote.id,
            "p_idempotency_key": command.idempotency_key, "p_commit_budget": commit_budget,
            "p_quantity": request.quantity, "p_total": quote.price_pence * request.quantity,
        })
        order = OrderRecord.model_validate(result["order"]) if result.get("order") else None
        code = PolicyCode(result["code"])
        return ReservationResult(
            accepted=bool(result["accepted"]), order=order, replayed=bool(result["replayed"]),
            decision=decision if code is PolicyCode.ALLOWED else PolicyDecision.reject(code),
        )

    async def _precheck(self, existing: OrderRecord | None, request: RequestRecord,
                        quote: QuoteRecord, commit_budget: bool,
                        reference: ReferencePriceRecord | None,
                        check: PolicyCheck) -> PolicyDecision:
        if existing is None and request.status not in OPEN_REQUEST_STATUSES:
            return PolicyDecision.reject(PolicyCode.REQUEST_CLOSED)
        needs_check = existing is None or (commit_budget and not existing.budget_committed
                                           and existing.status is OrderStatus.RESERVED
                                           and existing.request_id == request.id)
        if not needs_check:
            return PolicyDecision.allowed()
        mandate = await self.get_active_mandate()
        if mandate is None:
            return PolicyDecision.reject(PolicyCode.NO_ACTIVE_MANDATE)
        item = await self.get_inventory_item(str(quote.inventory_id))
        if item is None:
            raise NotFound("inventory", str(quote.inventory_id))
        return check(PolicyContext(
            mandate=mandate, request=request, quote=quote, inventory=item,
            recent_orders=await self._recent_orders(mandate.id,
                                                    existing.id if existing else None),
            reference=reference,
            stock_already_reserved=existing is not None,
            budget_already_committed=not commit_budget,
        ))

    async def finalize_payment(self, order_id: str, *, payment_reference: str,
                               reference: ReferencePriceRecord | None,
                               build_report: ReportBuilder
                               ) -> tuple[OrderRecord, ExecReportRecord | None]:
        order = await self.get_order(order_id)
        if order is None:
            raise NotFound("order", order_id)
        quotes = await self.list_quotes(order.request_id)
        paid_view = order.model_copy(update={"status": OrderStatus.PAID,
                                             "payment_reference": payment_reference})
        report = build_report(paid_view, quotes, reference)
        result = await self._rpc("checkout_atomic", {
            "p_order_id": order_id, "p_payment_reference": payment_reference,
            "p_report": report.model_dump(mode="json"),
        })
        final = OrderRecord.model_validate(result["order"])
        stored = ExecReportRecord.model_validate(result["report"]) if result.get("report") else None
        return final, stored

    async def release_failed_payment(self, order_id: str) -> OrderRecord:
        row = await self._rpc("release_failed_payment", {"p_order_id": order_id})
        if not row or row.get("id") is None:
            raise NotFound("order", order_id)
        return OrderRecord.model_validate(row)

    # ---------------------------------------------------------------- read model

    async def dashboard_snapshot(self, profile: str = "connected") -> DashboardSnapshot:
        newest = {"order": "created_at.desc"}
        mandate = await self.get_active_mandate()
        paid = await self._request("GET", "orders", params={"select": "id",
                                                             "status": "eq.paid"}) or []
        blocked = await self._request("GET", "events", params={
            "select": "id", "type": "eq.policy_rejected"}) or []
        saved = await self._request("GET", "exec_reports", params={"select": "saved_pence"}) or []
        latencies = await self._request("GET", "events", params={
            "select": "stage,latency_ms", "latency_ms": "not.is.null",
            "order": "created_at.desc", "limit": "2000"}) or []
        return DashboardSnapshot(
            profile=profile,
            mandate=mandate,
            merchants=await self.list_merchants(),
            inventory=await self.list_inventory(),
            requests=[RequestRecord.model_validate(r)
                      for r in await self._select("requests", limit="200", **newest)],
            quotes=[QuoteRecord.model_validate(r)
                    for r in await self._select("quotes", limit="150", **newest)],
            orders=[OrderRecord.model_validate(r)
                    for r in await self._select("orders", limit="150", **newest)],
            reports=[ExecReportRecord.model_validate(r)
                     for r in await self._select("exec_reports", limit="50", **newest)],
            events=await self.list_events(limit=400),
            counters=DashboardCounters(orders=len(paid), blocked_attempts=len(blocked),
                                       saved_pence=sum(int(r["saved_pence"]) for r in saved)),
            latency=stage_latency([(r["stage"], float(r["latency_ms"])) for r in latencies]),
        )

