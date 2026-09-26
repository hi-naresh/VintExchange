"""Pure deterministic execution policy.

`evaluate_quote` is synchronous and side-effect-free. It returns at the first
failed check, in the exact order the design specifies. Prompt text never reaches
this module: an instruction such as "ignore the budget" can shape a proposal but
cannot change a single input here.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import (
    InventoryRecord,
    MandateRecord,
    PolicyCode,
    PolicyDecision,
    QuoteRecord,
    ReferenceConfidence,
    ReferencePriceRecord,
    ReferenceSource,
    RequestRecord,
    RiskFlag,
)

POLICY_REASONS: dict[PolicyCode, str] = {
    PolicyCode.ALLOWED: "Quote passed every mandate check.",
    PolicyCode.KILL_SWITCH_ON: "The mandate kill switch is on; no orders may execute.",
    PolicyCode.MERCHANT_NOT_ALLOWED: "The merchant is not on the mandate's allowed list.",
    PolicyCode.OUT_OF_STOCK: "The merchant does not have enough stock for this quantity.",
    PolicyCode.BELOW_FLOOR: "The quoted price is below the merchant's floor price.",
    PolicyCode.ABOVE_REQUEST_MAX: "The quote total exceeds the request's maximum price.",
    PolicyCode.PER_ORDER_CAP_EXCEEDED: "The quote total exceeds the mandate's per-order cap.",
    PolicyCode.VELOCITY_LIMIT_EXCEEDED: "The mandate's orders-per-minute limit has been reached.",
    PolicyCode.BUDGET_EXCEEDED: "The remaining mandate budget does not cover the quote total.",
    PolicyCode.REFERENCE_PRICE_EXCEEDED: (
        "The quote total exceeds 150% of a high-confidence web reference price."
    ),
    PolicyCode.MALFORMED_LLM_OUTPUT: "The agent returned output that failed strict parsing.",
    PolicyCode.ROUND_LIMIT_REACHED: "Negotiation stopped at the three-round limit.",
    PolicyCode.NEGOTIATION_TIMEOUT: "Negotiation stopped at the 30-second deadline.",
    PolicyCode.MERCHANT_TIMEOUT: "The merchant agent did not respond before its deadline.",
    PolicyCode.MERCHANT_UNAVAILABLE: "The merchant agent could not be reached.",
    PolicyCode.UNKNOWN_SKU: "The merchant quoted a SKU it does not stock.",
    PolicyCode.OWNERSHIP_MISMATCH: "The inventory item does not belong to this merchant.",
    PolicyCode.REQUEST_CLOSED: "The request is no longer open for execution.",
    PolicyCode.NO_ACTIVE_MANDATE: "No active mandate exists.",
    PolicyCode.IDEMPOTENCY_KEY_REUSED: (
        "The idempotency key was already used for a different request or quote."
    ),
    PolicyCode.PAYMENT_FAILED: "The simulated payment failed; stock and budget were restored.",
}

REFERENCE_CEILING_PERCENT = 150


@dataclass(frozen=True, slots=True)
class PolicyContext:
    mandate: MandateRecord
    request: RequestRecord
    quote: QuoteRecord
    inventory: InventoryRecord
    recent_orders: int
    reference: ReferencePriceRecord | None = None
    # When converting an existing reservation into a checkout, stock (and possibly
    # budget) is already held by this order, so those guards are not re-applied.
    stock_already_reserved: bool = False
    budget_already_committed: bool = False


def quote_total(context: PolicyContext) -> int:
    price = context.quote.price_pence
    if price is None:
        raise ValueError("cannot evaluate a quote without a price")
    return price * context.request.quantity


def evaluate_quote(context: PolicyContext) -> PolicyDecision:
    mandate, request, quote, item = (
        context.mandate, context.request, context.quote, context.inventory
    )
    total = quote_total(context)

    if mandate.killed:
        return PolicyDecision.reject(PolicyCode.KILL_SWITCH_ON)
    if quote.merchant_id not in mandate.allowed_merchant_ids:
        return PolicyDecision.reject(PolicyCode.MERCHANT_NOT_ALLOWED)
    if not context.stock_already_reserved and item.stock < request.quantity:
        return PolicyDecision.reject(PolicyCode.OUT_OF_STOCK)
    if quote.price_pence < item.floor_price_pence:
        return PolicyDecision.reject(PolicyCode.BELOW_FLOOR)
    if total > request.max_price_pence:
        return PolicyDecision.reject(PolicyCode.ABOVE_REQUEST_MAX)
    if total > mandate.max_per_order_pence:
        return PolicyDecision.reject(PolicyCode.PER_ORDER_CAP_EXCEEDED)
    if context.recent_orders >= mandate.orders_per_minute:
        return PolicyDecision.reject(PolicyCode.VELOCITY_LIMIT_EXCEEDED)
    if not context.budget_already_committed and total > mandate.remaining_pence:
        return PolicyDecision.reject(PolicyCode.BUDGET_EXCEEDED)

    flags: list[RiskFlag] = []
    reference = context.reference
    if reference is None:
        flags.append(RiskFlag.NO_REFERENCE)
    else:
        reference_total = reference.price_pence * request.quantity
        above_ceiling = total * 100 > reference_total * REFERENCE_CEILING_PERCENT
        if reference.confidence is ReferenceConfidence.HIGH:
            if above_ceiling:
                return PolicyDecision.reject(PolicyCode.REFERENCE_PRICE_EXCEEDED)
        else:
            flags.append(RiskFlag.LOW_CONFIDENCE_REFERENCE)
            if reference.source is ReferenceSource.SEED_FALLBACK:
                flags.append(RiskFlag.SEEDED_REFERENCE)
            if above_ceiling:
                flags.append(RiskFlag.ABOVE_REFERENCE)
    return PolicyDecision.allowed_decision(flags)
