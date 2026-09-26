"""Async SQLite repository.

Every write transaction opens its own connection and starts with
`BEGIN IMMEDIATE`, so concurrent business transactions serialize on SQLite's
write lock. Stock and budget are additionally guarded by conditional UPDATEs
whose affected-row count must be exactly one.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import statistics
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

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
    StageLatency,
    iso,
    utcnow,
)
from app.domain.policy import PolicyContext
from app.repositories.protocol import (
    NotFound,
    PolicyCheck,
    ReportBuilder,
    RepositoryConflict,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_MS = 5_000
MAX_BUSY_RETRIES = 3
T = TypeVar("T")

TABLES_IN_DELETE_ORDER = (
    "exec_reports", "orders", "quotes", "events", "requests", "reference_prices",
    "inventory", "merchants", "mandates",
)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class _Abort(Exception):
    """Roll back the current transaction and return a policy decision."""

    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.code)
        self.decision = decision


def _is_busy(error: sqlite3.OperationalError) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


class SQLiteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    async def connect(cls, path: str | Path) -> SQLiteRepository:
        repository = cls(path)
        await repository.initialize()
        return repository

    # ------------------------------------------------------------------ plumbing

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000,
                                     isolation_level=None)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            yield db
        finally:
            await db.close()

    async def _transaction(self, work: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
        """Run `work` inside BEGIN IMMEDIATE, retrying busy conflicts briefly."""
        for attempt in range(1, MAX_BUSY_RETRIES + 1):
            try:
                async with self._connection() as db:
                    await db.execute("BEGIN IMMEDIATE")
                    try:
                        result = await work(db)
                    except BaseException:
                        await db.execute("ROLLBACK")
                        raise
                    await db.execute("COMMIT")
                    return result
            except sqlite3.OperationalError as error:
                if not _is_busy(error) or attempt == MAX_BUSY_RETRIES:
                    raise RepositoryConflict(f"database busy: {error}") from error
                await asyncio.sleep(0.01 * attempt)
            except sqlite3.IntegrityError as error:
                raise RepositoryConflict(str(error)) from error
        raise RepositoryConflict("unreachable")  # pragma: no cover

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self._connection() as db:
            cursor = await db.execute(sql, params)
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self._connection() as db:
            cursor = await db.execute(sql, params)
            return list(await cursor.fetchall())

    # ----------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connection() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.executescript(SCHEMA_PATH.read_text())

    async def reset(self) -> None:
        async def work(db: aiosqlite.Connection) -> None:
            for table in TABLES_IN_DELETE_ORDER:
                await db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
        await self._transaction(work)

    async def close(self) -> None:
        return None

    async def load_seed(self, merchants: list[dict[str, object]],
                        inventory: list[dict[str, object]], mandate_id: str,
                        mandate: MandateCreate) -> None:
        now = iso(utcnow())

        async def work(db: aiosqlite.Connection) -> None:
            for merchant in merchants:
                await db.execute(
                    "INSERT INTO merchants (id, name, rating, created_at) VALUES (?, ?, ?, ?)",
                    (merchant["id"], merchant["name"], merchant["rating"], now),
                )
            for item in inventory:
                await db.execute(
                    """INSERT INTO inventory (id, sku, merchant_id, title, category,
                       list_price_pence, floor_price_pence, stock, delivery_days,
                       external_id, external_price_pence)
                       VALUES (:id, :sku, :merchant_id, :title, :category,
                       :list_price_pence, :floor_price_pence, :stock, :delivery_days,
                       :external_id, :external_price_pence)""",
                    {"external_id": None, "external_price_pence": None, **item},
                )
            await self._insert_mandate(db, mandate_id, mandate, now)
        await self._transaction(work)

    # ------------------------------------------------------------------- mapping

    @staticmethod
    def _mandate(row: aiosqlite.Row) -> MandateRecord:
        data = dict(row)
        data["allowed_merchant_ids"] = json.loads(data["allowed_merchant_ids"])
        data["killed"] = bool(data["killed"])
        return MandateRecord.model_validate(data)

    @staticmethod
    def _order(row: aiosqlite.Row) -> OrderRecord:
        data = dict(row)
        data["budget_committed"] = bool(data["budget_committed"])
        return OrderRecord.model_validate(data)

    @staticmethod
    def _event(row: aiosqlite.Row) -> EventRecord:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return EventRecord.model_validate(data)

    @staticmethod
    def _report(row: aiosqlite.Row) -> ExecReportRecord:
        data = dict(row)
        data["reference_urls"] = json.loads(data["reference_urls"])
        return ExecReportRecord.model_validate(data)

    @staticmethod
    def _reference(row: aiosqlite.Row) -> ReferencePriceRecord:
        data = dict(row)
        data["source_urls"] = json.loads(data["source_urls"])
        return ReferencePriceRecord.model_validate(data)

    # ------------------------------------------------------------------- mandate

    @staticmethod
    async def _insert_mandate(db: aiosqlite.Connection, mandate_id: str,
                              command: MandateCreate, now: str) -> None:
        await db.execute("UPDATE mandates SET active = 0, updated_at = ? WHERE active = 1", (now,))
        await db.execute(
            """INSERT INTO mandates (id, budget_pence, spent_pence, max_per_order_pence,
               allowed_merchant_ids, orders_per_minute, killed, active, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?, ?, 0, 1, ?, ?)""",
            (mandate_id, command.budget_pence, command.max_per_order_pence,
             json.dumps(command.allowed_merchant_ids), command.orders_per_minute, now, now),
        )

    async def set_mandate(self, command: MandateCreate) -> MandateRecord:
        mandate_id = new_id("mandate")

        async def work(db: aiosqlite.Connection) -> None:
            known = await (await db.execute("SELECT id FROM merchants")).fetchall()
            unknown = set(command.allowed_merchant_ids) - {row["id"] for row in known}
            if unknown:
                raise NotFound("merchant", ", ".join(sorted(unknown)))
            await self._insert_mandate(db, mandate_id, command, iso(utcnow()))
        await self._transaction(work)
        mandate = await self.get_active_mandate()
        assert mandate is not None
        return mandate

    async def get_active_mandate(self) -> MandateRecord | None:
        row = await self._fetchone(
            "SELECT * FROM mandates WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
        )
        return self._mandate(row) if row else None

    async def set_killed(self, killed: bool) -> MandateRecord:
        async def work(db: aiosqlite.Connection) -> None:
            cursor = await db.execute(
                "UPDATE mandates SET killed = ?, updated_at = ? WHERE active = 1",
                (int(killed), iso(utcnow())),
            )
            if cursor.rowcount != 1:
                raise NotFound("mandate", "active")
        await self._transaction(work)
        mandate = await self.get_active_mandate()
        assert mandate is not None
        return mandate

    # ----------------------------------------------------------------- catalogue

    async def list_merchants(self) -> list[MerchantRecord]:
        rows = await self._fetchall("SELECT * FROM merchants ORDER BY id")
        return [MerchantRecord.model_validate(dict(r)) for r in rows]

    async def list_inventory(self, category: str | None = None, *,
                             in_stock_only: bool = False) -> list[InventoryRecord]:
        sql = "SELECT * FROM inventory WHERE 1 = 1"
        params: list[Any] = []
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if in_stock_only:
            sql += " AND stock > 0"
        rows = await self._fetchall(sql + " ORDER BY id", tuple(params))
        return [InventoryRecord.model_validate(dict(r)) for r in rows]

    async def get_inventory_item(self, inventory_id: str) -> InventoryRecord | None:
        row = await self._fetchone("SELECT * FROM inventory WHERE id = ?", (inventory_id,))
        return InventoryRecord.model_validate(dict(row)) if row else None

    async def set_list_price(self, inventory_id: str, price_pence: int) -> InventoryRecord:
        async def work(db: aiosqlite.Connection) -> None:
            cursor = await db.execute(
                """UPDATE inventory SET list_price_pence = ?
                   WHERE id = ? AND floor_price_pence <= ?""",
                (price_pence, inventory_id, price_pence),
            )
            if cursor.rowcount != 1:
                raise RepositoryConflict("reprice rejected by floor guard")
        await self._transaction(work)
        item = await self.get_inventory_item(inventory_id)
        if item is None:
            raise NotFound("inventory", inventory_id)
        return item

    # ------------------------------------------------------------------ requests

    async def create_request(self, command: RFQCreate, *, mandate_id: str) -> RequestRecord:
        request_id = new_id("req")
        now = iso(utcnow())

        async def work(db: aiosqlite.Connection) -> None:
            await db.execute(
                """INSERT INTO requests (id, mandate_id, query, category, max_price_pence,
                   quantity, deadline, status, round, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', 0, ?, ?)""",
                (request_id, mandate_id, command.query, command.category,
                 command.max_price_pence, command.quantity, iso(command.deadline), now, now),
            )
        await self._transaction(work)
        record = await self.get_request(request_id)
        assert record is not None
        return record

    async def get_request(self, request_id: str) -> RequestRecord | None:
        row = await self._fetchone("SELECT * FROM requests WHERE id = ?", (request_id,))
        return RequestRecord.model_validate(dict(row)) if row else None

    async def update_request(self, request_id: str, *, status: RequestStatus,
                             round: int | None = None) -> RequestRecord:
        async def work(db: aiosqlite.Connection) -> None:
            # A cancelled request stays cancelled, whatever a slower workflow writes.
            await db.execute(
                """UPDATE requests SET status = ?, round = COALESCE(?, round), updated_at = ?
                   WHERE id = ? AND status != 'cancelled'""",
                (status.value, round, iso(utcnow()), request_id),
            )
        await self._transaction(work)
        record = await self.get_request(request_id)
        if record is None:
            raise NotFound("request", request_id)
        return record

    async def cancel_request(self, request_id: str) -> RequestRecord | None:
        async def work(db: aiosqlite.Connection) -> int:
            cursor = await db.execute(
                """UPDATE requests SET status = 'cancelled', updated_at = ?
                   WHERE id = ? AND status IN ('requested', 'quoted', 'negotiating', 'resting')""",
                (iso(utcnow()), request_id),
            )
            return cursor.rowcount
        changed = await self._transaction(work)
        if await self.get_request(request_id) is None:
            raise NotFound("request", request_id)
        return await self.get_request(request_id) if changed == 1 else None

    async def list_resting_requests(self, category: str) -> list[RequestRecord]:
        rows = await self._fetchall(
            """SELECT * FROM requests WHERE status = 'resting' AND category = ?
               AND deadline > ? ORDER BY created_at, rowid""",
            (category, iso(utcnow())),
        )
        return [RequestRecord.model_validate(dict(r)) for r in rows]

    # -------------------------------------------------------------------- quotes

    async def create_quote(self, command: QuoteCreate, *, status: QuoteStatus,
                           rejection_reason: str | None,
                           origin: QuoteOrigin = QuoteOrigin.AGENT) -> QuoteRecord:
        quote_id = new_id("quote")

        async def work(db: aiosqlite.Connection) -> None:
            await db.execute(
                """INSERT INTO quotes (id, request_id, merchant_id, inventory_id, price_pence,
                   delivery_days, round, status, origin, rejection_reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (quote_id, command.request_id, command.merchant_id, command.inventory_id,
                 command.price_pence, command.delivery_days, command.round, status.value,
                 origin.value, rejection_reason, iso(utcnow())),
            )
        await self._transaction(work)
        quote = await self.get_quote(quote_id)
        assert quote is not None
        return quote

    async def record_failed_quote(self, *, request_id: str, merchant_id: str, round: int,
                                  reason: str) -> QuoteRecord:
        quote_id = new_id("quote")

        async def work(db: aiosqlite.Connection) -> None:
            await db.execute(
                """INSERT INTO quotes (id, request_id, merchant_id, inventory_id, price_pence,
                   delivery_days, round, status, origin, rejection_reason, created_at)
                   VALUES (?, ?, ?, NULL, NULL, NULL, ?, 'rejected', 'agent', ?, ?)""",
                (quote_id, request_id, merchant_id, round, reason, iso(utcnow())),
            )
        await self._transaction(work)
        quote = await self.get_quote(quote_id)
        assert quote is not None
        return quote

    async def get_quote(self, quote_id: str) -> QuoteRecord | None:
        row = await self._fetchone("SELECT * FROM quotes WHERE id = ?", (quote_id,))
        return QuoteRecord.model_validate(dict(row)) if row else None

    async def set_quote_status(self, quote_id: str, status: QuoteStatus) -> QuoteRecord:
        async def work(db: aiosqlite.Connection) -> None:
            cursor = await db.execute("UPDATE quotes SET status = ? WHERE id = ?",
                                      (status.value, quote_id))
            if cursor.rowcount != 1:
                raise NotFound("quote", quote_id)
        await self._transaction(work)
        quote = await self.get_quote(quote_id)
        assert quote is not None
        return quote

    async def list_quotes(self, request_id: str) -> list[QuoteRecord]:
        rows = await self._fetchall(
            "SELECT * FROM quotes WHERE request_id = ? ORDER BY created_at, rowid", (request_id,)
        )
        return [QuoteRecord.model_validate(dict(r)) for r in rows]

    # -------------------------------------------------------------------- events

    async def create_event(self, event: EventCreate) -> EventRecord:
        event_id = new_id("evt")
        now = iso(utcnow())
        async with self._connection() as db:
            await db.execute(
                """INSERT INTO events (id, created_at, type, request_id, payload, latency_ms,
                   stage) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_id, now, event.type, event.request_id,
                 json.dumps(event.payload, default=str), event.latency_ms, event.stage),
            )
        return EventRecord(id=event_id, created_at=now, **event.model_dump())

    async def list_events(self, request_id: str | None = None,
                          limit: int = 200) -> list[EventRecord]:
        if request_id is None:
            rows = await self._fetchall(
                "SELECT * FROM events ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            )
        else:
            rows = await self._fetchall(
                """SELECT * FROM events WHERE request_id = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (request_id, limit),
            )
        return [self._event(r) for r in rows]

    # ---------------------------------------------------------- reference prices

    async def get_reference_price(self, query_key: str, country: str = "GB",
                                  currency: str = "GBP") -> ReferencePriceRecord | None:
        row = await self._fetchone(
            """SELECT * FROM reference_prices
               WHERE query_key = ? AND country = ? AND currency = ?""",
            (query_key, country, currency),
        )
        return self._reference(row) if row else None

    async def upsert_reference_price(self, record: ReferencePriceCreate) -> ReferencePriceRecord:
        async def work(db: aiosqlite.Connection) -> None:
            await db.execute(
                """INSERT INTO reference_prices (id, query_key, product_name, country, currency,
                   price_pence, source, confidence, source_urls, fetched_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (query_key, country, currency) DO UPDATE SET
                     product_name = excluded.product_name, price_pence = excluded.price_pence,
                     source = excluded.source, confidence = excluded.confidence,
                     source_urls = excluded.source_urls, fetched_at = excluded.fetched_at,
                     expires_at = excluded.expires_at""",
                (new_id("ref"), record.query_key, record.product_name, record.country,
                 record.currency, record.price_pence, record.source.value,
                 record.confidence.value, json.dumps(record.source_urls), record.fetched_at,
                 record.expires_at),
            )
        await self._transaction(work)
        stored = await self.get_reference_price(record.query_key, record.country,
                                                record.currency)
        assert stored is not None
        return stored

    # ------------------------------------------------------------ orders/reports

    async def get_order(self, order_id: str) -> OrderRecord | None:
        row = await self._fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
        return self._order(row) if row else None

    async def get_order_by_idempotency_key(self, key: str) -> OrderRecord | None:
        row = await self._fetchone("SELECT * FROM orders WHERE idempotency_key = ?", (key,))
        return self._order(row) if row else None

    async def count_orders(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS n FROM orders")
        return int(row["n"]) if row else 0

    async def get_report(self, order_id: str) -> ExecReportRecord | None:
        row = await self._fetchone("SELECT * FROM exec_reports WHERE order_id = ?", (order_id,))
        return self._report(row) if row else None

    # ------------------------------------------------------ atomic transactions

    @staticmethod
    async def _one(db: aiosqlite.Connection, sql: str,
                   params: tuple[Any, ...]) -> aiosqlite.Row | None:
        return await (await db.execute(sql, params)).fetchone()

    async def _recent_orders(self, db: aiosqlite.Connection, mandate_id: str,
                             exclude_order_id: str | None = None) -> int:
        since = iso(utcnow() - timedelta(seconds=60))
        row = await self._one(
            db,
            """SELECT COUNT(*) AS n FROM orders WHERE mandate_id = ? AND created_at >= ?
               AND status IN ('reserved', 'paid') AND id IS NOT ?""",
            (mandate_id, since, exclude_order_id),
        )
        return int(row["n"]) if row else 0

    async def reserve_atomic(self, command: ReserveCommand, *, commit_budget: bool,
                             reference: ReferencePriceRecord | None,
                             check: PolicyCheck) -> ReservationResult:
        async def work(db: aiosqlite.Connection) -> ReservationResult:
            existing_row = await self._one(
                db, "SELECT * FROM orders WHERE idempotency_key = ?", (command.idempotency_key,)
            )
            if existing_row is not None:
                return await self._resume_existing(db, command, self._order(existing_row),
                                                   commit_budget, reference, check)

            request_row = await self._one(db, "SELECT * FROM requests WHERE id = ?",
                                          (command.request_id,))
            if request_row is None:
                raise NotFound("request", command.request_id)
            request = RequestRecord.model_validate(dict(request_row))
            quote_row = await self._one(db, "SELECT * FROM quotes WHERE id = ?",
                                        (command.quote_id,))
            if quote_row is None or quote_row["request_id"] != request.id:
                raise NotFound("quote", command.quote_id)
            quote = QuoteRecord.model_validate(dict(quote_row))
            if quote.status is QuoteStatus.REJECTED or quote.price_pence is None:
                raise RepositoryConflict(f"quote {quote.id} was rejected")
            if request.status not in OPEN_REQUEST_STATUSES:
                raise _Abort(PolicyDecision.reject(PolicyCode.REQUEST_CLOSED))
            mandate_row = await self._one(
                db, "SELECT * FROM mandates WHERE active = 1 ORDER BY created_at DESC LIMIT 1", ()
            )
            if mandate_row is None:
                raise _Abort(PolicyDecision.reject(PolicyCode.NO_ACTIVE_MANDATE))
            mandate = self._mandate(mandate_row)
            item_row = await self._one(db, "SELECT * FROM inventory WHERE id = ?",
                                       (quote.inventory_id,))
            if item_row is None:
                raise NotFound("inventory", str(quote.inventory_id))
            item = InventoryRecord.model_validate(dict(item_row))
            if item.merchant_id != quote.merchant_id:
                raise _Abort(PolicyDecision.reject(PolicyCode.OWNERSHIP_MISMATCH))

            context = PolicyContext(
                mandate=mandate, request=request, quote=quote, inventory=item,
                recent_orders=await self._recent_orders(db, mandate.id), reference=reference,
                budget_already_committed=not commit_budget,
            )
            decision = check(context)
            if not decision.allowed:
                raise _Abort(decision)

            total = quote.price_pence * request.quantity
            cursor = await db.execute(
                "UPDATE inventory SET stock = stock - ? WHERE id = ? AND stock >= ?",
                (request.quantity, item.id, request.quantity),
            )
            if cursor.rowcount != 1:
                raise _Abort(PolicyDecision.reject(PolicyCode.OUT_OF_STOCK))
            if commit_budget:
                await self._guard_budget(db, mandate.id, total)

            order_id = new_id("order")
            now = iso(utcnow())
            await db.execute(
                """INSERT INTO orders (id, request_id, quote_id, mandate_id, inventory_id,
                   idempotency_key, quantity, price_pence, budget_committed, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)""",
                (order_id, request.id, quote.id, mandate.id, item.id, command.idempotency_key,
                 request.quantity, quote.price_pence, int(commit_budget), now),
            )
            await db.execute(
                "UPDATE requests SET status = 'reserved', updated_at = ? WHERE id = ?",
                (now, request.id),
            )
            await db.execute("UPDATE quotes SET status = 'accepted' WHERE id = ?", (quote.id,))
            order_row = await self._one(db, "SELECT * FROM orders WHERE id = ?", (order_id,))
            assert order_row is not None
            return ReservationResult(accepted=True, decision=decision,
                                     order=self._order(order_row))

        try:
            return await self._transaction(work)
        except _Abort as abort:
            return ReservationResult(accepted=False, decision=abort.decision)

    @staticmethod
    async def _guard_budget(db: aiosqlite.Connection, mandate_id: str, total: int) -> None:
        cursor = await db.execute(
            """UPDATE mandates SET spent_pence = spent_pence + ?, updated_at = ?
               WHERE id = ? AND spent_pence + ? <= budget_pence""",
            (total, iso(utcnow()), mandate_id, total),
        )
        if cursor.rowcount != 1:
            raise _Abort(PolicyDecision.reject(PolicyCode.BUDGET_EXCEEDED))

    async def _resume_existing(self, db: aiosqlite.Connection, command: ReserveCommand,
                               order: OrderRecord, commit_budget: bool,
                               reference: ReferencePriceRecord | None,
                               check: PolicyCheck) -> ReservationResult:
        if order.request_id != command.request_id or order.quote_id != command.quote_id:
            raise _Abort(PolicyDecision.reject(PolicyCode.IDEMPOTENCY_KEY_REUSED))
        if order.status is OrderStatus.PAYMENT_FAILED:
            return ReservationResult(accepted=False, order=order, replayed=True,
                                     decision=PolicyDecision.reject(PolicyCode.PAYMENT_FAILED))
        if not commit_budget or order.budget_committed:
            return ReservationResult(accepted=True, order=order, replayed=True,
                                     decision=PolicyDecision.allowed())

        # Consume a stock-only reservation: re-run policy with stock already held.
        mandate_row = await self._one(db, "SELECT * FROM mandates WHERE id = ?",
                                      (order.mandate_id,))
        request_row = await self._one(db, "SELECT * FROM requests WHERE id = ?",
                                      (order.request_id,))
        quote_row = await self._one(db, "SELECT * FROM quotes WHERE id = ?", (order.quote_id,))
        item_row = await self._one(db, "SELECT * FROM inventory WHERE id = ?",
                                   (order.inventory_id,))
        if not (mandate_row and request_row and quote_row and item_row):
            raise RepositoryConflict(f"order {order.id} references missing rows")
        mandate = self._mandate(mandate_row)
        context = PolicyContext(
            mandate=mandate,
            request=RequestRecord.model_validate(dict(request_row)),
            quote=QuoteRecord.model_validate(dict(quote_row)),
            inventory=InventoryRecord.model_validate(dict(item_row)),
            recent_orders=await self._recent_orders(db, mandate.id, exclude_order_id=order.id),
            reference=reference,
            stock_already_reserved=True,
        )
        decision = check(context)
        if not decision.allowed:
            raise _Abort(decision)
        await self._guard_budget(db, mandate.id, order.total_pence)
        cursor = await db.execute(
            "UPDATE orders SET budget_committed = 1 WHERE id = ? AND budget_committed = 0",
            (order.id,),
        )
        if cursor.rowcount != 1:
            raise RepositoryConflict(f"order {order.id} budget already committed")
        row = await self._one(db, "SELECT * FROM orders WHERE id = ?", (order.id,))
        assert row is not None
        return ReservationResult(accepted=True, order=self._order(row), decision=decision)

    async def finalize_payment(self, order_id: str, *, payment_reference: str,
                               reference: ReferencePriceRecord | None,
                               build_report: ReportBuilder
                               ) -> tuple[OrderRecord, ExecReportRecord | None]:
        async def work(db: aiosqlite.Connection) -> tuple[OrderRecord, ExecReportRecord | None]:
            row = await self._one(db, "SELECT * FROM orders WHERE id = ?", (order_id,))
            if row is None:
                raise NotFound("order", order_id)
            order = self._order(row)
            if order.status is not OrderStatus.RESERVED:
                report_row = await self._one(
                    db, "SELECT * FROM exec_reports WHERE order_id = ?", (order_id,)
                )
                return order, self._report(report_row) if report_row else None
            if not order.budget_committed:
                raise RepositoryConflict(f"order {order_id} has no committed budget")
            now = iso(utcnow())
            cursor = await db.execute(
                """UPDATE orders SET status = 'paid', payment_reference = ?
                   WHERE id = ? AND status = 'reserved'""",
                (payment_reference, order_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryConflict(f"order {order_id} changed during payment")
            await db.execute("UPDATE requests SET status = 'paid', updated_at = ? WHERE id = ?",
                             (now, order.request_id))
            paid_row = await self._one(db, "SELECT * FROM orders WHERE id = ?", (order_id,))
            assert paid_row is not None
            paid = self._order(paid_row)
            quote_rows = await (await db.execute(
                "SELECT * FROM quotes WHERE request_id = ? ORDER BY created_at, rowid",
                (order.request_id,),
            )).fetchall()
            quotes = [QuoteRecord.model_validate(dict(r)) for r in quote_rows]
            report = build_report(paid, quotes, reference)
            await db.execute(
                """INSERT INTO exec_reports (id, order_id, paid_pence, best_quote_pence,
                   average_quote_pence, saved_pence, web_reference_pence, reference_source,
                   reference_confidence, reference_urls, summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id("report"), order_id, report.paid_pence, report.best_quote_pence,
                 report.average_quote_pence, report.saved_pence, report.web_reference_pence,
                 report.reference_source, report.reference_confidence,
                 json.dumps(report.reference_urls), report.summary, now),
            )
            report_row = await self._one(
                db, "SELECT * FROM exec_reports WHERE order_id = ?", (order_id,)
            )
            assert report_row is not None
            return paid, self._report(report_row)

        return await self._transaction(work)

    async def release_failed_payment(self, order_id: str) -> OrderRecord:
        async def work(db: aiosqlite.Connection) -> OrderRecord:
            row = await self._one(db, "SELECT * FROM orders WHERE id = ?", (order_id,))
            if row is None:
                raise NotFound("order", order_id)
            order = self._order(row)
            cursor = await db.execute(
                """UPDATE orders SET status = 'payment_failed'
                   WHERE id = ? AND status = 'reserved'""",
                (order_id,),
            )
            if cursor.rowcount == 1:  # release exactly once
                await db.execute("UPDATE inventory SET stock = stock + ? WHERE id = ?",
                                 (order.quantity, order.inventory_id))
                if order.budget_committed:
                    await db.execute(
                        """UPDATE mandates SET spent_pence = spent_pence - ?, updated_at = ?
                           WHERE id = ? AND spent_pence >= ?""",
                        (order.total_pence, iso(utcnow()), order.mandate_id, order.total_pence),
                    )
                await db.execute(
                    "UPDATE requests SET status = 'rejected', updated_at = ? WHERE id = ?",
                    (iso(utcnow()), order.request_id),
                )
            final = await self._one(db, "SELECT * FROM orders WHERE id = ?", (order_id,))
            assert final is not None
            return self._order(final)

        return await self._transaction(work)

    # ---------------------------------------------------------------- read model

    async def dashboard_snapshot(self, profile: str = "offline") -> DashboardSnapshot:
        async with self._connection() as db:
            async def rows(sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
                return list(await (await db.execute(sql, params)).fetchall())

            mandate_rows = await rows(
                "SELECT * FROM mandates WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
            )
            merchants = await rows("SELECT * FROM merchants ORDER BY id")
            inventory = await rows("SELECT * FROM inventory ORDER BY id")
            requests = await rows(_newest("requests", 50))
            quotes = await rows(_newest("quotes", 150))
            orders = await rows(_newest("orders", 50))
            reports = await rows(_newest("exec_reports", 50))
            events = await rows(_newest("events", 400))
            paid = await rows("SELECT COUNT(*) AS n FROM orders WHERE status = 'paid'")
            blocked = await rows("SELECT COUNT(*) AS n FROM events WHERE type = 'policy_rejected'")
            saved = await rows("SELECT COALESCE(SUM(saved_pence), 0) AS n FROM exec_reports")
            latencies = await rows(
                "SELECT stage, latency_ms FROM events WHERE latency_ms IS NOT NULL"
            )

        return DashboardSnapshot(
            profile=profile,
            mandate=self._mandate(mandate_rows[0]) if mandate_rows else None,
            merchants=[MerchantRecord.model_validate(dict(r)) for r in merchants],
            inventory=[InventoryRecord.model_validate(dict(r)) for r in inventory],
            requests=[RequestRecord.model_validate(dict(r)) for r in requests],
            quotes=[QuoteRecord.model_validate(dict(r)) for r in quotes],
            orders=[self._order(r) for r in orders],
            reports=[self._report(r) for r in reports],
            events=[self._event(r) for r in events],
            counters=DashboardCounters(orders=int(paid[0]["n"]),
                                       blocked_attempts=int(blocked[0]["n"]),
                                       saved_pence=int(saved[0]["n"])),
            latency=stage_latency([(r["stage"], float(r["latency_ms"])) for r in latencies]),
        )


def _newest(table: str, limit: int) -> str:
    return f"SELECT * FROM {table} ORDER BY created_at DESC, rowid DESC LIMIT {int(limit)}"  # noqa: S608


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile."""
    if not sorted_values:
        raise ValueError("no values")
    rank = max(1, min(len(sorted_values), math.ceil(len(sorted_values) * pct / 100)))
    return sorted_values[rank - 1]


def stage_latency(samples: list[tuple[str, float]]) -> dict[str, StageLatency]:
    result: dict[str, StageLatency] = {}
    for stage in ("llm", "policy", "database"):
        values = sorted(v for s, v in samples if s == stage)
        result[stage] = StageLatency(
            count=len(values),
            median_ms=round(statistics.median(values), 3) if values else None,
            p95_ms=round(percentile(values, 95), 3) if values else None,
        )
    return result
