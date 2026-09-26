"""Reliability proofs: stock race, idempotent checkout, concurrent budget."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.domain.models import (
    CheckoutCommand,
    MandateCreate,
    OrderStatus,
    PolicyCode,
    QuoteCreate,
    QuoteStatus,
    ReserveCommand,
)
from app.repositories.seed import DEMO_MANDATE_ID
from app.services.commerce import CommerceService
from app.services.events import EventRecorder
from app.services.payments import SimulatedPaymentGateway
from tests.conftest import headphone_rfq


@pytest.fixture
def gateway():
    return SimulatedPaymentGateway()


@pytest.fixture
def service(repository, gateway):
    return CommerceService(repository, EventRecorder(repository), payment=gateway)


async def make_quote(repository, *, inventory_id: str, merchant_id: str, price: int,
                     category: str = "audio", max_pence: int = 17_000):
    request = await repository.create_request(
        headphone_rfq(max_pence=max_pence, category=category), mandate_id=DEMO_MANDATE_ID
    )
    return await repository.create_quote(
        QuoteCreate(request_id=request.id, merchant_id=merchant_id, inventory_id=inventory_id,
                    price_pence=price, delivery_days=1, round=1),
        status=QuoteStatus.VALID, rejection_reason=None,
    )


@pytest.fixture
async def one_stock_quote(repository):
    return await make_quote(repository, inventory_id="inventory-circuit-buds-ltd",
                            merchant_id="merchant-circuit", price=4_900, category="earbuds")


@pytest.fixture
async def reservable_quote(repository):
    return await make_quote(repository, inventory_id="inventory-bassline-pro",
                            merchant_id="merchant-bassline", price=16_200)


@pytest.fixture
async def two_requests_one_budget(repository):
    await repository.set_mandate(MandateCreate(
        budget_pence=20_000, max_per_order_pence=20_000,
        allowed_merchant_ids=["merchant-circuit", "merchant-bassline"], orders_per_minute=10))
    quotes = [
        await make_quote(repository, inventory_id="inventory-circuit-q45",
                         merchant_id="merchant-circuit", price=15_800),
        await make_quote(repository, inventory_id="inventory-bassline-pro",
                         merchant_id="merchant-bassline", price=16_200),
    ]
    return [SimpleNamespace(id=q.request_id, quote_id=q.id) for q in quotes]


async def test_twenty_reservations_for_one_unit_have_one_winner(service, one_stock_quote):
    results = await asyncio.gather(*[
        service.reserve(ReserveCommand(request_id=one_stock_quote.request_id,
                                       quote_id=one_stock_quote.id,
                                       idempotency_key=f"race-{i}"))
        for i in range(20)
    ])
    assert sum(result.accepted for result in results) == 1
    item = await service.repository.get_inventory_item(one_stock_quote.inventory_id)
    assert item.stock == 0


async def test_twenty_contenders_across_requests_never_oversell(service, repository):
    quotes = [await make_quote(repository, inventory_id="inventory-circuit-buds-ltd",
                               merchant_id="merchant-circuit", price=4_900,
                               category="earbuds") for _ in range(20)]
    results = await asyncio.gather(*[
        service.reserve(ReserveCommand(request_id=q.request_id, quote_id=q.id,
                                       idempotency_key=f"multi-{i}"))
        for i, q in enumerate(quotes)
    ])
    assert sum(r.accepted for r in results) == 1
    losers = {r.decision.code for r in results if not r.accepted}
    assert losers <= {PolicyCode.OUT_OF_STOCK, PolicyCode.VELOCITY_LIMIT_EXCEEDED}
    assert (await repository.get_inventory_item("inventory-circuit-buds-ltd")).stock == 0


async def test_same_checkout_key_creates_one_order(service, reservable_quote, gateway):
    commands = [CheckoutCommand(request_id=reservable_quote.request_id,
                                quote_id=reservable_quote.id,
                                idempotency_key="demo-key") for _ in range(20)]
    results = await asyncio.gather(*(service.checkout(c) for c in commands))
    assert len({result.order.id for result in results}) == 1
    assert await service.repository.count_orders() == 1
    assert all(r.accepted and r.payment.simulated for r in results)
    assert gateway.charge_count == 1
    mandate = await service.repository.get_active_mandate()
    assert mandate.spent_pence == 16_200


async def test_concurrent_orders_never_exceed_budget(service, two_requests_one_budget):
    results = await asyncio.gather(*[
        service.execute(request.id, request.quote_id, f"budget-{request.id}")
        for request in two_requests_one_budget
    ])
    mandate = await service.repository.get_active_mandate()
    assert sum(result.accepted for result in results) == 1
    assert mandate.spent_pence <= mandate.budget_pence
    rejected = [r for r in results if not r.accepted]
    assert rejected[0].decision.code is PolicyCode.BUDGET_EXCEEDED


async def test_reserve_then_checkout_consumes_reservation(service, reservable_quote):
    command = ReserveCommand(request_id=reservable_quote.request_id,
                             quote_id=reservable_quote.id, idempotency_key="two-step")
    reserved = await service.reserve(command)
    assert reserved.accepted and not reserved.order.budget_committed
    assert (await service.repository.get_active_mandate()).spent_pence == 0
    paid = await service.checkout(CheckoutCommand(**command.model_dump()))
    assert paid.accepted and paid.order.id == reserved.order.id
    assert paid.order.status is OrderStatus.PAID
    assert (await service.repository.get_active_mandate()).spent_pence == 16_200
    item = await service.repository.get_inventory_item("inventory-bassline-pro")
    assert item.stock == 4  # stock was taken once, at reservation


async def test_failed_payment_restores_stock_and_budget_once(repository, reservable_quote):
    gateway = SimulatedPaymentGateway(fail_keys={"will-fail"})
    service = CommerceService(repository, EventRecorder(repository), payment=gateway)
    command = CheckoutCommand(request_id=reservable_quote.request_id,
                              quote_id=reservable_quote.id, idempotency_key="will-fail")
    results = await asyncio.gather(*(service.checkout(command) for _ in range(5)))
    assert not any(r.accepted for r in results)
    assert (await repository.get_active_mandate()).spent_pence == 0
    assert (await repository.get_inventory_item("inventory-bassline-pro")).stock == 5
    assert gateway.charge_count == 1


async def test_idempotency_key_cannot_be_reused_for_another_quote(service, repository,
                                                                  reservable_quote):
    await service.checkout(CheckoutCommand(request_id=reservable_quote.request_id,
                                           quote_id=reservable_quote.id,
                                           idempotency_key="shared"))
    other = await make_quote(repository, inventory_id="inventory-circuit-q45",
                             merchant_id="merchant-circuit", price=15_800)
    result = await service.checkout(CheckoutCommand(request_id=other.request_id,
                                                    quote_id=other.id, idempotency_key="shared"))
    assert not result.accepted
    assert result.decision.code is PolicyCode.IDEMPOTENCY_KEY_REUSED


async def test_paid_order_writes_exactly_one_report(service, reservable_quote):
    result = await service.checkout(CheckoutCommand(request_id=reservable_quote.request_id,
                                                    quote_id=reservable_quote.id,
                                                    idempotency_key="report-key"))
    assert result.report.paid_pence == 16_200
    assert result.report.saved_pence == 0
    assert "simulated" in result.report.summary.lower()
