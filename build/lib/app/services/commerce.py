"""Reservation and idempotent checkout.

Every path funnels into the repository's atomic operations, which re-read
current state and re-run the pure policy inside one transaction. The payment
gateway is called only after the reservation commits; a failed simulated payment
restores stock and budget exactly once.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from app.domain.models import (
    CheckoutCommand,
    CheckoutResult,
    InventoryRecord,
    OrderRecord,
    PolicyCode,
    PolicyDecision,
    ReferencePriceRecord,
    RequestRecord,
    ReservationResult,
    ReserveCommand,
)
from app.domain.policy import PolicyContext, evaluate_quote
from app.market.protocol import MarketPriceProvider
from app.repositories.protocol import NotFound, Repository
from app.services.events import EventRecorder
from app.services.payments import PaymentGateway, SimulatedPaymentGateway
from app.services.reports import build_exec_report


class _TimedCheck:
    """Wrap the pure policy so its latency is reported separately from the database."""

    def __init__(self) -> None:
        self.elapsed_ms = 0.0
        self.calls = 0

    def __call__(self, context: PolicyContext) -> PolicyDecision:
        started = time.monotonic_ns()
        try:
            return evaluate_quote(context)
        finally:
            self.calls += 1
            self.elapsed_ms += (time.monotonic_ns() - started) / 1_000_000


class OrderSink(Protocol):
    async def create_draft_order(self, order: OrderRecord,
                                 item: InventoryRecord) -> dict[str, str]: ...


class CommerceService:
    def __init__(self, repository: Repository, recorder: EventRecorder,
                 payment: PaymentGateway | None = None,
                 market: MarketPriceProvider | None = None,
                 order_sink: OrderSink | None = None) -> None:
        self.repository = repository
        self.recorder = recorder
        self.payment = payment or SimulatedPaymentGateway()
        self.market = market
        self.order_sink = order_sink
        self._pending: set[asyncio.Task[None]] = set()

    async def _mirror_order(self, order: OrderRecord) -> None:
        """Mirror a paid fill to the merchant platform. Never affects the fill."""
        assert self.order_sink is not None
        started = time.monotonic_ns()
        try:
            item = await self.repository.get_inventory_item(order.inventory_id)
            if item is None:
                raise RuntimeError(f"inventory {order.inventory_id} missing")
            draft = await self.order_sink.create_draft_order(order, item)
        except Exception as error:  # noqa: BLE001 - mirror failures are reported, not raised
            await self.recorder.emit("shopify_draft_order_failed", request_id=order.request_id,
                                     order_id=order.id, error=str(error)[:200])
            return
        await self.recorder.emit(
            "shopify_draft_order", request_id=order.request_id,
            latency_ms=round((time.monotonic_ns() - started) / 1e6, 3), order_id=order.id,
            draft_order_id=draft.get("id"), draft_order_name=draft.get("name"),
        )

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

    async def _reference(self, request_id: str,
                         reference: ReferencePriceRecord | None) -> ReferencePriceRecord | None:
        if reference is not None or self.market is None:
            return reference
        request: RequestRecord | None = await self.repository.get_request(request_id)
        if request is None:
            raise NotFound("request", request_id)
        return await self.market.peek(request.query)

    async def _atomic_reserve(self, command: ReserveCommand, *, commit_budget: bool,
                              reference: ReferencePriceRecord | None) -> ReservationResult:
        check = _TimedCheck()
        started = time.monotonic_ns()
        result = await self.repository.reserve_atomic(
            command, commit_budget=commit_budget, reference=reference, check=check
        )
        db_ms = (time.monotonic_ns() - started) / 1_000_000 - check.elapsed_ms
        if check.calls:
            await self.recorder.emit(
                "policy_checked", stage="policy", request_id=command.request_id,
                latency_ms=round(check.elapsed_ms, 3), code=result.decision.code.value,
                flags=[f.value for f in result.decision.flags],
            )
        if not result.accepted:
            await self.recorder.emit(
                "policy_rejected", stage="policy", request_id=command.request_id,
                code=result.decision.code.value, reason=result.decision.reason,
                quote_id=command.quote_id,
            )
        elif not result.replayed:
            await self.recorder.emit(
                "order_reserved", stage="database", request_id=command.request_id,
                latency_ms=round(max(db_ms, 0.0), 3), order_id=result.order.id,
                budget_committed=result.order.budget_committed,
            )
        return result

    async def reserve(self, command: ReserveCommand,
                      reference: ReferencePriceRecord | None = None) -> ReservationResult:
        reference = await self._reference(command.request_id, reference)
        return await self._atomic_reserve(command, commit_budget=False, reference=reference)

    async def checkout(self, command: CheckoutCommand,
                       reference: ReferencePriceRecord | None = None) -> CheckoutResult:
        reference = await self._reference(command.request_id, reference)
        reservation = await self._atomic_reserve(
            ReserveCommand(**command.model_dump()), commit_budget=True, reference=reference
        )
        order = reservation.order
        if not reservation.accepted or order is None:
            return CheckoutResult(accepted=False, decision=reservation.decision, order=order,
                                  replayed=reservation.replayed)

        payment = await self.payment.charge(command.idempotency_key, order.total_pence)
        if not payment.succeeded:
            released = await self.repository.release_failed_payment(order.id)
            await self.recorder.emit("payment_failed", stage="system",
                                     request_id=order.request_id, order_id=order.id,
                                     simulated=True)
            return CheckoutResult(accepted=False, order=released, payment=payment,
                                  decision=PolicyDecision.reject(PolicyCode.PAYMENT_FAILED))

        started = time.monotonic_ns()
        was_reserved = order.status.value == "reserved"
        paid, report = await self.repository.finalize_payment(
            order.id, payment_reference=payment.reference or "", reference=reference,
            build_report=build_exec_report,
        )
        if was_reserved and report is not None and report.order_id == paid.id:
            elapsed = (time.monotonic_ns() - started) / 1_000_000
            await self.recorder.emit(
                "order_paid", stage="database", request_id=paid.request_id,
                latency_ms=round(elapsed, 3), order_id=paid.id, paid_pence=report.paid_pence,
                saved_pence=report.saved_pence, simulated=True,
            )
            if self.order_sink is not None:
                task = asyncio.create_task(self._mirror_order(paid))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        return CheckoutResult(accepted=paid.status.value == "paid", decision=reservation.decision,
                              order=paid, payment=payment, report=report,
                              replayed=reservation.replayed)

    async def execute(self, request_id: str, quote_id: str, idempotency_key: str,
                      reference: ReferencePriceRecord | None = None) -> CheckoutResult:
        """Orchestration path: one atomic reserve-and-commit, then simulated payment.

        Stock and budget are taken in the same transaction, so a fill that fails the
        budget check never leaves a stranded stock reservation behind.
        """
        reference = await self._reference(request_id, reference)
        return await self.checkout(
            CheckoutCommand(request_id=request_id, quote_id=quote_id,
                            idempotency_key=idempotency_key),
            reference=reference,
        )
