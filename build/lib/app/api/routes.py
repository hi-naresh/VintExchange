"""HTTP/tool endpoints. Every mutation is typed, policy-checked, and evented."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query

from app.api.dependencies import get_services
from app.domain.models import (
    AmendCommand,
    CheckoutCommand,
    CheckoutResult,
    CounterCreate,
    DashboardSnapshot,
    ExecReportRecord,
    InventoryRecord,
    KillCommand,
    MandateCreate,
    MandateRecord,
    PolicyCode,
    PolicyDecision,
    PolicyRejected,
    QuoteCreate,
    QuoteRecord,
    ReferencePriceRecord,
    RepriceCommand,
    RepriceOutcome,
    RequestOutcome,
    RequestRecord,
    RequestStatus,
    ReservationResult,
    ReserveCommand,
    RestockCommand,
    RFQCreate,
    TextRFQ,
)
from app.market.protocol import normalize_product_key
from app.repositories.protocol import NotFound
from app.repositories.seed import FLASH_SALES
from app.services.container import Services

router = APIRouter()
ServicesDep = Annotated[Services, Depends(get_services)]


@router.post("/mandate", response_model=MandateRecord, tags=["mandate"])
async def set_mandate(command: MandateCreate, services: ServicesDep) -> MandateRecord:
    mandate = await services.repository.set_mandate(command)
    await services.recorder.emit("mandate_set", mandate_id=mandate.id,
                                 budget_pence=mandate.budget_pence,
                                 max_per_order_pence=mandate.max_per_order_pence,
                                 allowed_merchant_ids=mandate.allowed_merchant_ids,
                                 orders_per_minute=mandate.orders_per_minute)
    return mandate


@router.post("/kill", response_model=MandateRecord, tags=["mandate"])
async def kill(command: KillCommand, services: ServicesDep) -> MandateRecord:
    mandate = await services.repository.set_killed(command.killed)
    await services.recorder.emit("kill_switch", mandate_id=mandate.id, killed=mandate.killed)
    return mandate


@router.post("/requests", response_model=RequestOutcome, tags=["buyer"])
async def submit_request(
    command: RFQCreate | TextRFQ, services: ServicesDep,
    x_agent_name: Annotated[str | None, Header(max_length=60,
                                               description="Name of the calling agent, "
                                               "e.g. a Grok Bot teammate")] = None,
) -> RequestOutcome:
    """Any agent can trade here: send plain words or a structured RFQ.

    Your mandate still decides; set X-Agent-Name so the exchange shows who placed it.
    """
    if isinstance(command, TextRFQ):
        outcome = await services.orchestrator.submit_text(command.text, agent=x_agent_name)
    else:
        outcome = await services.orchestrator.submit_rfq(command)
    if (outcome.request.status is RequestStatus.REJECTED and outcome.decision is not None
            and not outcome.decision.allowed):
        raise PolicyRejected(outcome.decision, outcome.request.id)
    return outcome


@router.post("/requests/{request_id}/cancel", response_model=RequestRecord, tags=["buyer"])
async def cancel_request(
    request_id: str, services: ServicesDep,
    x_agent_name: Annotated[str | None, Header(max_length=60)] = None,
) -> RequestRecord:
    """Cancel an open or resting request. Returns 422 REQUEST_CLOSED if it already filled."""
    return await services.orchestrator.cancel(request_id, agent=x_agent_name)


@router.post("/requests/{request_id}/amend", response_model=RequestOutcome, tags=["buyer"])
async def amend_request(
    request_id: str, command: AmendCommand, services: ServicesDep,
    x_agent_name: Annotated[str | None, Header(max_length=60)] = None,
) -> RequestOutcome:
    """Change a waiting order's limit or quantity; it fills at once if it now crosses."""
    await services.orchestrator.amend(request_id, command, agent=x_agent_name)
    await services.repricing.fill_if_crossed(request_id)
    request = await services.repository.get_request(request_id)
    assert request is not None
    order = next((o for o in (await services.repository.dashboard_snapshot()).orders
                  if o.request_id == request_id), None)
    return RequestOutcome(request=request,
                          quotes=await services.repository.list_quotes(request_id),
                          events=list(reversed(await services.repository.list_events(
                              request_id))),
                          order=order)


@router.post("/simulate/market", tags=["demo"])
async def simulate_market(services: ServicesDep) -> dict[str, Any]:
    """Several buyer agents trade at once under one mandate, then a merchant flash sale."""
    from app.services.simulation import run_market

    return await run_market(services)


@router.post("/simulate/arena", tags=["demo"])
async def simulate_arena(services: ServicesDep) -> dict[str, Any]:
    """Run a fresh isolated fixed-budget market; no external orders are created."""
    from app.services.simulation import run_arena

    return await run_arena(services)


@router.get("/orderbook/{category}", tags=["market"])
async def order_book(category: str, services: ServicesDep) -> dict[str, Any]:
    """Live book for one category: waiting buyer bids vs merchant asks, per piece."""
    snapshot = await services.repository.dashboard_snapshot(services.settings.profile)
    open_statuses = {"requested", "quoted", "negotiating", "resting"}
    bids = sorted(
        ({"request_id": r.id, "price_each_pence": r.max_price_pence // r.quantity,
          "quantity": r.quantity, "agent": r.agent, "since": r.created_at}
         for r in snapshot.requests
         if r.category == category and r.status.value in open_statuses),
        key=lambda b: (-b["price_each_pence"], b["since"]))
    asks = sorted(
        ({"inventory_id": i.id, "merchant_id": i.merchant_id, "title": i.title,
          "price_each_pence": i.list_price_pence, "stock": i.stock}
         for i in snapshot.inventory if i.category == category and i.stock > 0),
        key=lambda a: a["price_each_pence"])
    items = {i.id for i in snapshot.inventory if i.category == category}
    trades = [{"order_id": o.id, "inventory_id": o.inventory_id, "quantity": o.quantity,
               "price_each_pence": o.price_pence, "at": o.created_at}
              for o in snapshot.orders if o.inventory_id in items and o.status.value == "paid"]
    spread = (asks[0]["price_each_pence"] - bids[0]["price_each_pence"]
              if asks and bids else None)
    return {"category": category, "bids": bids, "asks": asks, "spread_pence": spread,
            "trades": trades[:25], "payment_simulated": True}


@router.get("/inventory", response_model=list[InventoryRecord], tags=["merchant"])
async def inventory(services: ServicesDep,
                    category: Annotated[str | None, Query(max_length=80)] = None
                    ) -> list[InventoryRecord]:
    return await services.repository.list_inventory(category, in_stock_only=True)


@router.post("/quotes", response_model=QuoteRecord, tags=["merchant"])
async def submit_quote(command: QuoteCreate, services: ServicesDep) -> QuoteRecord:
    return await services.orchestrator.submit_quote(command)


@router.post("/quotes/{quote_id}/counter", response_model=QuoteRecord, tags=["buyer"])
async def counter_quote(quote_id: str, command: CounterCreate,
                        services: ServicesDep) -> QuoteRecord:
    return await services.orchestrator.counter_quote(quote_id, command)


@router.post("/reserve", response_model=ReservationResult, tags=["execution"])
async def reserve(command: ReserveCommand, services: ServicesDep) -> ReservationResult:
    result = await services.commerce.reserve(command)
    if not result.accepted:
        raise PolicyRejected(result.decision, command.request_id)
    return result


@router.post("/checkout", response_model=CheckoutResult, tags=["execution"])
async def checkout(command: CheckoutCommand, services: ServicesDep) -> CheckoutResult:
    result = await services.commerce.checkout(command)
    if not result.accepted:
        raise PolicyRejected(result.decision, command.request_id)
    return result


@router.post("/merchants/{merchant_id}/reprice", response_model=RepriceOutcome,
             tags=["merchant"])
async def reprice(merchant_id: str, command: RepriceCommand,
                  services: ServicesDep) -> RepriceOutcome:
    return await services.repricing.reprice(merchant_id, command)


@router.post("/merchants/{merchant_id}/restock", response_model=InventoryRecord,
             tags=["merchant"])
async def restock(merchant_id: str, command: RestockCommand,
                  services: ServicesDep) -> InventoryRecord:
    item = await services.repository.get_inventory_item(command.inventory_id)
    if item is None:
        raise NotFound("inventory", command.inventory_id)
    if item.merchant_id != merchant_id:
        raise PolicyRejected(PolicyDecision.reject(PolicyCode.OWNERSHIP_MISMATCH))
    item = await services.repository.add_stock(item.id, command.quantity)
    await services.recorder.emit("restock", merchant_id=merchant_id, inventory_id=item.id,
                                 quantity=command.quantity, stock=item.stock)
    return item


@router.get("/report/{order_id}", response_model=ExecReportRecord, tags=["reporting"])
async def report(order_id: str, services: ServicesDep) -> ExecReportRecord:
    return await services.reports.get(order_id)


@router.get("/dashboard", response_model=DashboardSnapshot, tags=["reporting"])
async def dashboard(services: ServicesDep) -> DashboardSnapshot:
    return await services.repository.dashboard_snapshot(services.settings.profile)


@router.get("/reference-prices/{query_key}", response_model=ReferencePriceRecord,
            tags=["reporting"])
async def reference_price(query_key: str, services: ServicesDep) -> ReferencePriceRecord:
    reference = await services.market.peek(query_key)
    if reference is None:
        raise NotFound("reference price", normalize_product_key(query_key))
    return reference


@router.get("/health", tags=["system"])
async def health(services: ServicesDep) -> dict[str, Any]:
    try:
        await services.repository.get_active_mandate()
        database = "ready"
    except Exception as error:  # noqa: BLE001 - health must report, not raise
        database = f"error: {type(error).__name__}"
    settings = services.settings
    return {
        "status": "ok" if database == "ready" else "degraded",
        "profile": settings.profile,
        "database": database,
        "llm": {"mode": settings.llm_mode, "model": services.llm.model_name},
        "market": settings.market_mode,
        "catalog": services.catalog_description,
        "shopify_draft_orders": services.order_sink is not None,
        "analytics": "posthog" if services.event_sinks else "off",
        "payment": "simulated",
    }


@router.get("/bootstrap", tags=["system"])
async def bootstrap(services: ServicesDep) -> dict[str, Any]:
    """Browser bootstrap. Contains only publishable, select-only configuration."""
    settings = services.settings
    connected = settings.profile == "connected"
    return {
        "profile": settings.profile,
        "realtime": connected,
        "supabase_url": settings.supabase_url if connected else None,
        "supabase_publishable_key": settings.supabase_publishable_key if connected else None,
        "catalog": services.catalog_description,
        "payment_simulated": True,
        "take_rate_bps": settings.take_rate_bps,
        "flash_sale": FLASH_SALES[settings.demo_theme],
    }
