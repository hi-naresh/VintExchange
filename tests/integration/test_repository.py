import pytest

from app.domain.models import (
    EventCreate,
    MandateCreate,
    QuoteCreate,
    QuoteStatus,
    RequestStatus,
)
from app.repositories.protocol import NotFound, RepositoryConflict


async def test_seed_has_three_merchants_and_six_skus(repository):
    snapshot = await repository.dashboard_snapshot()
    assert len(snapshot.merchants) == 3
    assert len(snapshot.inventory) == 6
    assert snapshot.mandate is not None and snapshot.mandate.budget_pence == 30_000


async def test_quote_references_request_merchant_and_inventory(repository, seeded_request):
    with pytest.raises(RepositoryConflict):
        await repository.create_quote(
            QuoteCreate(request_id=seeded_request.id, merchant_id="unknown",
                        inventory_id="unknown", price_pence=14_800,
                        delivery_days=1, round=1),
            status=QuoteStatus.VALID,
            rejection_reason=None,
        )


async def test_request_quote_crud_round_trip(repository, seeded_request):
    assert seeded_request.status is RequestStatus.REQUESTED
    quote = await repository.create_quote(
        QuoteCreate(request_id=seeded_request.id, merchant_id="merchant-bassline",
                    inventory_id="inventory-bassline-pro", price_pence=16_200,
                    delivery_days=1, round=1),
        status=QuoteStatus.VALID, rejection_reason=None,
    )
    failed = await repository.record_failed_quote(
        request_id=seeded_request.id, merchant_id="merchant-aurora", round=1,
        reason="MALFORMED_LLM_OUTPUT",
    )
    quotes = await repository.list_quotes(seeded_request.id)
    assert [q.id for q in quotes] == [quote.id, failed.id]
    assert failed.price_pence is None and failed.status is QuoteStatus.REJECTED
    updated = await repository.update_request(seeded_request.id,
                                              status=RequestStatus.RESTING, round=2)
    assert updated.status is RequestStatus.RESTING and updated.round == 2
    assert [r.id for r in await repository.list_resting_requests("audio")] == [updated.id]


async def test_database_rejects_negative_stock_and_below_floor_price(repository):
    with pytest.raises(RepositoryConflict):
        await repository.set_list_price("inventory-bassline-pro", 14_700)
    item = await repository.set_list_price("inventory-bassline-pro", 14_800)
    assert item.list_price_pence == 14_800


async def test_mandate_replacement_and_kill_switch(repository):
    mandate = await repository.set_mandate(MandateCreate(
        budget_pence=50_000, max_per_order_pence=20_000,
        allowed_merchant_ids=["merchant-aurora"], orders_per_minute=2))
    assert (await repository.get_active_mandate()).id == mandate.id
    killed = await repository.set_killed(True)
    assert killed.killed is True
    with pytest.raises(NotFound):
        await repository.set_mandate(MandateCreate(
            budget_pence=50_000, max_per_order_pence=20_000,
            allowed_merchant_ids=["merchant-nobody"], orders_per_minute=2))


async def test_events_are_newest_first_with_payload(repository):
    await repository.create_event(EventCreate(type="a", stage="system", payload={"n": 1}))
    await repository.create_event(EventCreate(type="b", stage="llm", latency_ms=5.0))
    events = (await repository.dashboard_snapshot()).events
    assert [e.type for e in events] == ["b", "a"]
    assert events[1].payload == {"n": 1}
