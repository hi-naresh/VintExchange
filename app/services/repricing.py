"""Merchant repricing and resting (limit) request fills.

The new price is committed first; then the oldest eligible resting requests in
the same category are each sent through the normal policy + transaction path.
One request losing the last unit or failing a mandate check never stops the rest.
"""

from __future__ import annotations

from app.domain.models import (
    PolicyCode,
    PolicyDecision,
    PolicyRejected,
    QuoteCreate,
    QuoteOrigin,
    QuoteStatus,
    RepriceCommand,
    RepriceOutcome,
    RequestStatus,
)
from app.market.protocol import MarketPriceProvider
from app.repositories.protocol import NotFound, Repository, RepositoryConflict
from app.services.commerce import CommerceService
from app.services.events import EventRecorder


class RepricingService:
    def __init__(self, repository: Repository, commerce: CommerceService,
                 recorder: EventRecorder, market: MarketPriceProvider | None = None) -> None:
        self.repository = repository
        self.commerce = commerce
        self.recorder = recorder
        self.market = market

    async def fill_if_crossed(self, request_id: str) -> str | None:
        """After a buyer raises a limit: fill at the best current price if it now fits.

        Returns the order id on a fill. Uses the same policy + transaction path.
        """
        request = await self.repository.get_request(request_id)
        mandate = await self.repository.get_active_mandate()
        if request is None or mandate is None or request.status is not RequestStatus.RESTING:
            return None
        items = [i for i in await self.repository.list_inventory(request.category,
                                                                  in_stock_only=True)
                 if i.merchant_id in mandate.allowed_merchant_ids
                 and i.stock >= request.quantity
                 and i.list_price_pence * request.quantity <= request.max_price_pence]
        if not items:
            return None
        best = min(items, key=lambda i: (i.list_price_pence, i.delivery_days))
        quote = await self.repository.create_quote(
            QuoteCreate(request_id=request.id, merchant_id=best.merchant_id,
                        inventory_id=best.id, price_pence=best.list_price_pence,
                        delivery_days=best.delivery_days, round=max(1, request.round)),
            status=QuoteStatus.VALID, rejection_reason=None, origin=QuoteOrigin.REPRICE,
        )
        result = await self.commerce.execute(
            request.id, quote.id, f"amend:{request.id}:{best.id}:{best.list_price_pence}")
        if result.accepted and result.order is not None:
            await self.recorder.emit("limit_fill", request_id=request.id,
                                     order_id=result.order.id, inventory_id=best.id,
                                     price_pence=best.list_price_pence)
            return result.order.id
        await self.recorder.emit("limit_skip", request_id=request.id,
                                 code=result.decision.code.value, inventory_id=best.id)
        return None

    async def reprice(self, merchant_id: str, command: RepriceCommand) -> RepriceOutcome:
        item = await self.repository.get_inventory_item(command.inventory_id)
        if item is None:
            raise NotFound("inventory", command.inventory_id)
        if item.merchant_id != merchant_id:
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.OWNERSHIP_MISMATCH))
        if command.price_pence < item.floor_price_pence:
            await self.recorder.emit("policy_rejected", stage="policy",
                                     code=PolicyCode.BELOW_FLOOR.value, merchant_id=merchant_id,
                                     inventory_id=item.id, price_pence=command.price_pence)
            raise PolicyRejected(PolicyDecision.reject(PolicyCode.BELOW_FLOOR))

        item = await self.repository.set_list_price(item.id, command.price_pence)
        await self.recorder.emit("reprice", merchant_id=merchant_id, inventory_id=item.id,
                                 price_pence=item.list_price_pence)

        filled: list[str] = []
        skipped: list[dict[str, str]] = []
        for request in await self.repository.list_resting_requests(item.category):
            total = command.price_pence * request.quantity

            async def skip(code: PolicyCode, request_id: str = request.id) -> None:
                skipped.append({"request_id": request_id, "code": code.value})
                await self.recorder.emit("limit_skip", request_id=request_id, code=code.value,
                                         inventory_id=item.id)

            if total > request.max_price_pence:
                await skip(PolicyCode.ABOVE_REQUEST_MAX)
                continue
            current = await self.repository.get_inventory_item(item.id)
            if current is None or current.stock < request.quantity:
                await skip(PolicyCode.OUT_OF_STOCK)
                continue
            quote = await self.repository.create_quote(
                QuoteCreate(request_id=request.id, merchant_id=merchant_id,
                            inventory_id=item.id, price_pence=command.price_pence,
                            delivery_days=item.delivery_days, round=max(1, request.round)),
                status=QuoteStatus.VALID, rejection_reason=None, origin=QuoteOrigin.REPRICE,
            )
            key = f"reprice:{request.id}:{item.id}:{command.price_pence}"
            try:
                result = await self.commerce.execute(request.id, quote.id, key)
            except (RepositoryConflict, NotFound):
                await skip(PolicyCode.REQUEST_CLOSED)
                continue
            if result.accepted and result.order is not None:
                filled.append(result.order.id)
                await self.recorder.emit("limit_fill", request_id=request.id,
                                         order_id=result.order.id, inventory_id=item.id,
                                         price_pence=command.price_pence)
            else:
                await skip(result.decision.code)

        final = await self.repository.get_inventory_item(item.id)
        assert final is not None
        return RepriceOutcome(inventory=final, filled_order_ids=filled, skipped=skipped)
