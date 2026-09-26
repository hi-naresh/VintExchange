"""Persistence contract shared by the SQLite and Supabase implementations.

Ordinary reads/writes are simple methods. Money- and stock-moving operations are
single atomic methods (`reserve_atomic`, `finalize_payment`,
`release_failed_payment`) so each backend can enforce guards at its own
transaction boundary (SQLite `BEGIN IMMEDIATE`, Postgres RPC with `FOR UPDATE`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.domain.models import (
    DashboardSnapshot,
    EventCreate,
    EventRecord,
    ExecReportCreate,
    ExecReportRecord,
    InventoryRecord,
    MandateCreate,
    MandateRecord,
    MerchantRecord,
    OrderRecord,
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
)
from app.domain.policy import PolicyContext


class RepositoryError(Exception):
    pass


class NotFound(RepositoryError):
    def __init__(self, entity: str, entity_id: str) -> None:
        super().__init__(f"{entity} {entity_id} not found")
        self.entity = entity
        self.entity_id = entity_id


class RepositoryConflict(RepositoryError):
    """Constraint or transaction conflict (maps to HTTP 409)."""


PolicyCheck = Callable[[PolicyContext], PolicyDecision]
ReportBuilder = Callable[
    [OrderRecord, list[QuoteRecord], ReferencePriceRecord | None], ExecReportCreate
]


class Repository(Protocol):
    # lifecycle ------------------------------------------------------------------
    async def initialize(self) -> None: ...
    async def reset(self) -> None: ...
    async def close(self) -> None: ...
    async def load_seed(self, merchants: list[dict[str, object]],
                        inventory: list[dict[str, object]], mandate_id: str,
                        mandate: MandateCreate) -> None: ...

    # mandate --------------------------------------------------------------------
    async def set_mandate(self, command: MandateCreate) -> MandateRecord: ...
    async def get_active_mandate(self) -> MandateRecord | None: ...
    async def set_killed(self, killed: bool) -> MandateRecord: ...

    # catalogue ------------------------------------------------------------------
    async def list_merchants(self) -> list[MerchantRecord]: ...
    async def list_inventory(self, category: str | None = None, *,
                             in_stock_only: bool = False) -> list[InventoryRecord]: ...
    async def get_inventory_item(self, inventory_id: str) -> InventoryRecord | None: ...
    async def set_list_price(self, inventory_id: str, price_pence: int) -> InventoryRecord: ...

    # requests -------------------------------------------------------------------
    async def create_request(self, command: RFQCreate, *, mandate_id: str) -> RequestRecord: ...
    async def get_request(self, request_id: str) -> RequestRecord | None: ...
    async def update_request(self, request_id: str, *, status: RequestStatus,
                             round: int | None = None) -> RequestRecord: ...
    async def list_resting_requests(self, category: str) -> list[RequestRecord]: ...
    async def cancel_request(self, request_id: str) -> RequestRecord | None:
        """Atomically cancel an open request; None if it already filled or closed."""
        ...

    # quotes ---------------------------------------------------------------------
    async def create_quote(self, command: QuoteCreate, *, status: QuoteStatus,
                           rejection_reason: str | None,
                           origin: QuoteOrigin = QuoteOrigin.AGENT) -> QuoteRecord: ...
    async def record_failed_quote(self, *, request_id: str, merchant_id: str, round: int,
                                  reason: str) -> QuoteRecord: ...
    async def get_quote(self, quote_id: str) -> QuoteRecord | None: ...
    async def set_quote_status(self, quote_id: str, status: QuoteStatus) -> QuoteRecord: ...
    async def list_quotes(self, request_id: str) -> list[QuoteRecord]: ...

    # events ---------------------------------------------------------------------
    async def create_event(self, event: EventCreate) -> EventRecord: ...
    async def list_events(self, request_id: str | None = None,
                          limit: int = 200) -> list[EventRecord]: ...

    # reference prices -----------------------------------------------------------
    async def get_reference_price(self, query_key: str, country: str = "GB",
                                  currency: str = "GBP") -> ReferencePriceRecord | None: ...
    async def upsert_reference_price(self, record: ReferencePriceCreate
                                     ) -> ReferencePriceRecord: ...

    # orders and reports ---------------------------------------------------------
    async def get_order(self, order_id: str) -> OrderRecord | None: ...
    async def get_order_by_idempotency_key(self, key: str) -> OrderRecord | None: ...
    async def count_orders(self) -> int: ...
    async def get_report(self, order_id: str) -> ExecReportRecord | None: ...

    # atomic business transactions ----------------------------------------------
    async def reserve_atomic(self, command: ReserveCommand, *, commit_budget: bool,
                             reference: ReferencePriceRecord | None,
                             check: PolicyCheck) -> ReservationResult: ...
    async def finalize_payment(self, order_id: str, *, payment_reference: str,
                               reference: ReferencePriceRecord | None,
                               build_report: ReportBuilder
                               ) -> tuple[OrderRecord, ExecReportRecord | None]: ...
    async def release_failed_payment(self, order_id: str) -> OrderRecord: ...

    # read model -----------------------------------------------------------------
    async def dashboard_snapshot(self, profile: str = "offline") -> DashboardSnapshot: ...
