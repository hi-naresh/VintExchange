"""RFQ fan-out and bounded negotiation.

Grok proposes; deterministic code disposes. Merchant agents are asked
concurrently, each under its own timeout, while one market-reference lookup runs
in parallel. Every proposal is validated by code before persistence; only
`CommerceService` (policy + atomic transaction) may execute. Negotiation stops
after `max_rounds` total rounds or the global deadline, whichever comes first.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.agents.protocol import LLMClient, LLMError, LLMOutputError, QuoteProposal
from app.config import Settings
from app.domain.models import (
    OPEN_REQUEST_STATUSES,
    AmendCommand,
    CounterCreate,
    InventoryRecord,
    MerchantRecord,
    PolicyCode,
    PolicyDecision,
    PolicyRejected,
    QuoteCreate,
    QuoteOrigin,
    QuoteRecord,
    QuoteStatus,
    ReferencePriceRecord,
    RequestOutcome,
    RequestRecord,
    RequestStatus,
    RFQCreate,
    default_deadline,
)
from app.market.fake import seed_reference
from app.market.protocol import MarketPriceProvider
from app.repositories.protocol import NotFound, Repository
from app.services.commerce import CommerceService
from app.services.events import EventRecorder


@dataclass(frozen=True, slots=True)
class _Validation:
    status: QuoteStatus
    reason: PolicyCode | None
    item: InventoryRecord | None


def validate_proposal(merchant_id: str, proposal: QuoteProposal,
                      catalogue: list[InventoryRecord], quantity: int) -> _Validation:
    """Code-side validation of a merchant agent's proposal."""
    item = next((i for i in catalogue if i.sku == proposal.sku), None)
    if item is None:
        return _Validation(QuoteStatus.REJECTED, PolicyCode.UNKNOWN_SKU, None)
    if item.merchant_id != merchant_id:
        return _Validation(QuoteStatus.REJECTED, PolicyCode.OWNERSHIP_MISMATCH, item)
    if item.stock < quantity:
        return _Validation(QuoteStatus.REJECTED, PolicyCode.OUT_OF_STOCK, item)
    if proposal.price_pence < item.floor_price_pence:
        return _Validation(QuoteStatus.REJECTED, PolicyCode.BELOW_FLOOR, item)
    return _Validation(QuoteStatus.VALID, None, item)


class OrchestrationService:
    def __init__(self, repository: Repository, llm: LLMClient, market: MarketPriceProvider,
                 commerce: CommerceService, recorder: EventRecorder,
                 settings: Settings) -> None:
        self.repository = repository
        self.llm = llm
        self.market = market
        self.commerce = commerce
        self.recorder = recorder
        self.settings = settings

    # ------------------------------------------------------------------ entry points

    async def submit_text(self, text: str, agent: str | None = None) -> RequestOutcome:
        started = time.monotonic_ns()
        try:
            proposal = await asyncio.wait_for(self.llm.parse_rfq(text),
                                              self.settings.merchant_timeout_seconds)
        except LLMOutputError as error:
            await self.recorder.emit("policy_rejected", stage="policy",
                                     code=error.code.value, operation=error.operation)
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.MALFORMED_LLM_OUTPUT)) from None
        except (TimeoutError, LLMError):
            await self.recorder.emit("rfq_parse_failed", stage="llm")
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.MERCHANT_UNAVAILABLE)) from None
        command = RFQCreate(
            query=proposal.query, category=proposal.category,
            max_price_pence=proposal.max_price_pence, quantity=proposal.quantity,
            deadline=proposal.deadline or default_deadline(),
        )
        return await self.submit_rfq(command, source_text=text, agent=agent,
                                     parse_ms=(time.monotonic_ns() - started) / 1e6)

    async def submit_rfq(self, command: RFQCreate, *, source_text: str | None = None,
                         parse_ms: float | None = None,
                         agent: str | None = None) -> RequestOutcome:
        started = time.monotonic()
        deadline = started + self.settings.negotiation_timeout_seconds
        mandate = await self.repository.get_active_mandate()
        if mandate is None:
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.NO_ACTIVE_MANDATE))
        request = await self.repository.create_request(command, mandate_id=mandate.id,
                                                       agent=agent)
        await self.recorder.emit("request_created", request_id=request.id,
                                 query=request.query, category=request.category,
                                 max_price_pence=request.max_price_pence,
                                 quantity=request.quantity)
        if source_text is not None:
            await self.recorder.emit(
                "intent_parsed", stage="llm", request_id=request.id,
                latency_ms=round(parse_ms, 3) if parse_ms is not None else None,
                text=source_text[:300], agent=agent, query=request.query,
                category=request.category,
                max_price_pence=request.max_price_pence, quantity=request.quantity,
            )

        if mandate.killed:
            decision = PolicyDecision.reject(PolicyCode.KILL_SWITCH_ON)
            request = await self.repository.update_request(request.id,
                                                           status=RequestStatus.REJECTED)
            await self.recorder.emit("policy_rejected", stage="policy", request_id=request.id,
                                     code=decision.code.value, reason=decision.reason)
            return await self._outcome(request, decision=decision)

        # One market lookup, started before merchant fan-out.
        market_task = asyncio.create_task(self._lookup_reference(request))

        merchants = {m.id: m for m in await self.repository.list_merchants()
                     if m.id in mandate.allowed_merchant_ids}
        in_stock = [i for i in await self.repository.list_inventory(request.category,
                                                                    in_stock_only=True)
                    if i.merchant_id in merchants and i.stock >= request.quantity]
        if not in_stock:
            request = await self.repository.update_request(request.id,
                                                           status=RequestStatus.OUT_OF_STOCK)
            await self.recorder.emit("out_of_stock", request_id=request.id,
                                     category=request.category,
                                     code=PolicyCode.OUT_OF_STOCK.value)
            reference = await market_task
            return await self._outcome(
                request, decision=PolicyDecision.reject(PolicyCode.OUT_OF_STOCK),
                reference=reference,
            )

        catalogue = {mid: [i for i in in_stock if i.merchant_id == mid] for mid in merchants}
        participants = [merchants[mid] for mid, items in catalogue.items() if items]
        full_catalogue = await self.repository.list_inventory(request.category)
        return await self._negotiate(request, participants, full_catalogue, catalogue,
                                     market_task, deadline)

    # ------------------------------------------------------------------ negotiation

    async def _negotiate(self, request: RequestRecord, participants: list[MerchantRecord],
                         full_catalogue: list[InventoryRecord],
                         catalogue: dict[str, list[InventoryRecord]],
                         market_task: asyncio.Task[ReferencePriceRecord | None],
                         deadline: float) -> RequestOutcome:
        ratings = {m.id: m.rating for m in participants}
        round_no, counter_pence = 1, None
        stop_code: PolicyCode | None = None
        best: QuoteRecord | None = None
        while True:
            if time.monotonic() >= deadline:
                stop_code = PolicyCode.NEGOTIATION_TIMEOUT
                break
            status = RequestStatus.QUOTED if round_no == 1 else RequestStatus.NEGOTIATING
            request = await self.repository.update_request(request.id, status=status,
                                                           round=round_no)
            if request.status is RequestStatus.CANCELLED:
                await self.recorder.emit("negotiation_stopped", request_id=request.id,
                                         code=PolicyCode.REQUEST_CLOSED.value,
                                         reason="cancelled by the buyer")
                return await self._outcome(request, reference=await market_task)
            quotes = await asyncio.gather(*(
                self._one_merchant(m, catalogue[m.id], full_catalogue, request, round_no,
                                   counter_pence, deadline)
                for m in participants
            ), return_exceptions=True)
            valid = [q for q in quotes if isinstance(q, QuoteRecord)
                     and q.status is QuoteStatus.VALID]
            best = min(valid, key=lambda q: (q.price_pence * request.quantity,
                                             q.delivery_days, -ratings[q.merchant_id]),
                       default=None)
            if best is not None and best.price_pence * request.quantity <= request.max_price_pence:
                return await self._execute(request, best, market_task)
            if best is None and time.monotonic() >= deadline:
                stop_code = PolicyCode.NEGOTIATION_TIMEOUT
                break
            if best is None or not self.settings.negotiation_enabled:
                break
            if round_no >= self.settings.max_rounds:
                stop_code = PolicyCode.ROUND_LIMIT_REACHED
                break
            if time.monotonic() >= deadline:
                stop_code = PolicyCode.NEGOTIATION_TIMEOUT
                break
            counter_pence = await self._buyer_counter(request, best, round_no, deadline)
            if counter_pence is None:
                break
            participants = [m for m in participants
                            if any(q.merchant_id == m.id for q in valid)]
            round_no += 1

        if stop_code is not None:
            await self.recorder.emit("negotiation_stopped", request_id=request.id,
                                     code=stop_code.value, round=request.round)
        request = await self.repository.update_request(request.id, status=RequestStatus.RESTING)
        if request.status is RequestStatus.CANCELLED:
            return await self._outcome(request, reference=await market_task)
        await self.recorder.emit(
            "request_resting", request_id=request.id,
            best_price_pence=best.price_pence if best else None,
            max_price_pence=request.max_price_pence,
        )
        reference = await market_task
        return await self._outcome(request, reference=reference)

    async def _buyer_counter(self, request: RequestRecord, best: QuoteRecord, round_no: int,
                             deadline: float) -> int | None:
        remaining = max(0.0, deadline - time.monotonic())
        timeout = min(self.settings.merchant_timeout_seconds, remaining)
        started = time.monotonic_ns()
        try:
            proposal = await asyncio.wait_for(self.llm.counter(request, best, round_no), timeout)
        except LLMOutputError:
            await self.recorder.emit("counter_rejected", stage="policy", request_id=request.id,
                                     code=PolicyCode.MALFORMED_LLM_OUTPUT.value)
            return None
        except (TimeoutError, LLMError):
            await self.recorder.emit("counter_failed", stage="llm", request_id=request.id)
            return None
        finally:
            await self.recorder.emit(
                "buyer_counter", stage="llm", request_id=request.id,
                latency_ms=round((time.monotonic_ns() - started) / 1e6, 3), round=round_no,
            )
        if proposal.action != "counter" or proposal.price_pence is None:
            await self.recorder.emit("buyer_walked", request_id=request.id,
                                     action=proposal.action, round=round_no)
            return None
        # Code, not the agent, bounds the counter to the request maximum.
        price = min(proposal.price_pence, request.max_price_pence // request.quantity)
        await self.repository.set_quote_status(best.id, QuoteStatus.COUNTERED)
        await self.recorder.emit("counter_sent", request_id=request.id, quote_id=best.id,
                                 price_pence=price, round=round_no)
        return price

    async def _one_merchant(self, merchant: MerchantRecord, catalogue: list[InventoryRecord],
                            full_catalogue: list[InventoryRecord], request: RequestRecord,
                            round_no: int, counter_pence: int | None,
                            deadline: float) -> QuoteRecord:
        remaining = max(0.0, deadline - time.monotonic())
        timeout = min(self.settings.merchant_timeout_seconds, remaining)
        started = time.monotonic_ns()
        failure: PolicyCode | None = None
        proposal: QuoteProposal | None = None
        try:
            proposal = await asyncio.wait_for(
                self.llm.quote(merchant, catalogue, request, round_no, counter_pence), timeout
            )
        except TimeoutError:
            failure = PolicyCode.MERCHANT_TIMEOUT
        except LLMOutputError:
            failure = PolicyCode.MALFORMED_LLM_OUTPUT
        except LLMError:
            failure = PolicyCode.MERCHANT_UNAVAILABLE
        await self.recorder.emit(
            "merchant_quote", stage="llm", request_id=request.id,
            latency_ms=round((time.monotonic_ns() - started) / 1e6, 3),
            merchant_id=merchant.id, round=round_no, ok=failure is None,
        )
        if failure is not None or proposal is None:
            reason = (failure or PolicyCode.MALFORMED_LLM_OUTPUT).value
            quote = await self.repository.record_failed_quote(
                request_id=request.id, merchant_id=merchant.id, round=round_no, reason=reason)
            await self.recorder.emit("quote_rejected", stage="policy", request_id=request.id,
                                     merchant_id=merchant.id, code=reason, quote_id=quote.id)
            return quote

        own_items = [i for i in full_catalogue if i.merchant_id == merchant.id]
        check_started = time.monotonic_ns()
        verdict = validate_proposal(merchant.id, proposal, own_items, request.quantity)
        check_ms = round((time.monotonic_ns() - check_started) / 1e6, 3)
        if verdict.item is None:
            quote = await self.repository.record_failed_quote(
                request_id=request.id, merchant_id=merchant.id, round=round_no,
                reason=PolicyCode.UNKNOWN_SKU.value)
        else:
            quote = await self.repository.create_quote(
                QuoteCreate(request_id=request.id, merchant_id=merchant.id,
                            inventory_id=verdict.item.id, price_pence=proposal.price_pence,
                            delivery_days=proposal.delivery_days, round=round_no),
                status=verdict.status,
                rejection_reason=verdict.reason.value if verdict.reason else None,
            )
        if verdict.status is QuoteStatus.VALID:
            await self.recorder.emit("quote_received", stage="policy", request_id=request.id,
                                     latency_ms=check_ms, merchant_id=merchant.id,
                                     quote_id=quote.id, price_pence=quote.price_pence,
                                     round=round_no)
        else:
            await self.recorder.emit("quote_rejected", stage="policy", request_id=request.id,
                                     latency_ms=check_ms, merchant_id=merchant.id,
                                     quote_id=quote.id, code=verdict.reason.value,
                                     proposed_price_pence=proposal.price_pence)
        return quote

    # ------------------------------------------------------------------- execution

    async def _execute(self, request: RequestRecord, best: QuoteRecord,
                       market_task: asyncio.Task[ReferencePriceRecord | None]
                       ) -> RequestOutcome:
        reference = await market_task
        result = await self.commerce.execute(request.id, best.id, f"rfq:{request.id}:{best.id}",
                                             reference=reference)
        refreshed = await self.repository.get_request(request.id)
        assert refreshed is not None
        if not result.accepted and refreshed.status in OPEN_REQUEST_STATUSES:
            refreshed = await self.repository.update_request(request.id,
                                                             status=RequestStatus.REJECTED)
        return await self._outcome(refreshed, decision=result.decision, reference=reference,
                                   order=result.order, payment=result.payment,
                                   report=result.report)

    async def _lookup_reference(self, request: RequestRecord) -> ReferencePriceRecord | None:
        started = time.monotonic_ns()
        outcome = "ok"
        try:
            async with asyncio.timeout(self.settings.market_timeout_seconds):
                reference = await self.market.lookup(request.query)
        except TimeoutError:
            outcome = "timeout"
            reference = seed_reference(request.query)
        except Exception as error:  # noqa: BLE001 - never block a fill on market data
            outcome = type(error).__name__
            reference = seed_reference(request.query)
        await self.recorder.emit(
            "market_reference", request_id=request.id,
            latency_ms=round((time.monotonic_ns() - started) / 1e6, 3), outcome=outcome,
            price_pence=reference.price_pence if reference else None,
            source=reference.source.value if reference else None,
            confidence=reference.confidence.value if reference else None,
        )
        return reference

    async def _outcome(self, request: RequestRecord, **extra: object) -> RequestOutcome:
        reference = extra.pop("reference", None)
        return RequestOutcome(
            request=request,
            quotes=await self.repository.list_quotes(request.id),
            events=list(reversed(await self.repository.list_events(request.id))),
            reference_price=reference,
            **extra,
        )

    # --------------------------------------------------------------- tool endpoints

    async def amend(self, request_id: str, command: AmendCommand,
                    agent: str | None = None) -> RequestRecord:
        """Change a waiting order's limit or quantity. Filled orders can't change."""
        current = await self.repository.get_request(request_id)
        if current is None:
            raise NotFound("request", request_id)
        quantity = command.quantity or current.quantity
        if command.max_price_pence is not None:
            max_total = command.max_price_pence
        else:  # keep the same price per piece when only the quantity changes
            max_total = (current.max_price_pence // current.quantity) * quantity
        request = await self.repository.amend_request(request_id, max_price_pence=max_total,
                                                      quantity=quantity)
        if request is None:
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.REQUEST_CLOSED), request_id)
        await self.recorder.emit("request_amended", request_id=request_id, agent=agent,
                                 max_price_pence=request.max_price_pence,
                                 quantity=request.quantity)
        return request

    async def cancel(self, request_id: str, agent: str | None = None) -> RequestRecord:
        """Cancel an open or resting request. A fill that already won stays filled."""
        request = await self.repository.cancel_request(request_id)
        if request is None:
            current = await self.repository.get_request(request_id)
            await self.recorder.emit("cancel_rejected", request_id=request_id,
                                     code=PolicyCode.REQUEST_CLOSED.value,
                                     status=current.status.value if current else None)
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.REQUEST_CLOSED), request_id)
        await self.recorder.emit("request_cancelled", request_id=request_id, agent=agent)
        return request

    async def submit_quote(self, command: QuoteCreate) -> QuoteRecord:
        """Validate and persist a merchant quote submitted through the API."""
        request = await self.repository.get_request(command.request_id)
        if request is None:
            raise NotFound("request", command.request_id)
        item = await self.repository.get_inventory_item(command.inventory_id)
        if item is None:
            raise NotFound("inventory", command.inventory_id)
        if request.status not in OPEN_REQUEST_STATUSES:
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.REQUEST_CLOSED), request.id)
        reason: PolicyCode | None = None
        if item.merchant_id != command.merchant_id:
            reason = PolicyCode.OWNERSHIP_MISMATCH
        elif item.stock < request.quantity:
            reason = PolicyCode.OUT_OF_STOCK
        elif command.price_pence < item.floor_price_pence:
            reason = PolicyCode.BELOW_FLOOR
        quote = await self.repository.create_quote(
            command, status=QuoteStatus.REJECTED if reason else QuoteStatus.VALID,
            rejection_reason=reason.value if reason else None, origin=QuoteOrigin.API,
        )
        await self.recorder.emit("quote_rejected" if reason else "quote_received",
                                 stage="policy", request_id=request.id, quote_id=quote.id,
                                 merchant_id=command.merchant_id,
                                 code=reason.value if reason else None,
                                 price_pence=command.price_pence)
        if reason:
            raise PolicyRejected(PolicyDecision.reject(reason), request.id)
        return quote

    async def counter_quote(self, quote_id: str, command: CounterCreate) -> QuoteRecord:
        """Record a buyer counter and ask that merchant's agent for a response."""
        quote = await self.repository.get_quote(quote_id)
        if quote is None:
            raise NotFound("quote", quote_id)
        request = await self.repository.get_request(quote.request_id)
        assert request is not None
        if request.status not in OPEN_REQUEST_STATUSES or quote.status is QuoteStatus.REJECTED:
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.REQUEST_CLOSED), request.id)
        next_round = max(request.round, quote.round) + 1
        if next_round > self.settings.max_rounds:
            await self.recorder.emit("negotiation_stopped", request_id=request.id,
                                     code=PolicyCode.ROUND_LIMIT_REACHED.value)
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.ROUND_LIMIT_REACHED),
                                 request.id)
        merchants = {m.id: m for m in await self.repository.list_merchants()}
        full_catalogue = await self.repository.list_inventory(request.category)
        own_in_stock = [i for i in full_catalogue
                        if i.merchant_id == quote.merchant_id and i.stock > 0]
        await self.repository.set_quote_status(quote.id, QuoteStatus.COUNTERED)
        request = await self.repository.update_request(
            request.id, status=RequestStatus.NEGOTIATING, round=next_round)
        await self.recorder.emit("counter_sent", request_id=request.id, quote_id=quote.id,
                                 price_pence=command.price_pence, round=next_round)
        deadline = time.monotonic() + self.settings.merchant_timeout_seconds
        return await self._one_merchant(merchants[quote.merchant_id], own_in_stock,
                                        full_catalogue, request, next_round,
                                        command.price_pence, deadline)
