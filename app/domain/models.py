"""Typed commands, records, enums, and responses.

All money is integer pence. Input (command) models forbid extra fields so a
client can never smuggle in status, spent amount, floor price, stock, merchant
authorization, or order identifiers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

MoneyPence = Annotated[StrictInt, Field(ge=0)]
PositivePence = Annotated[StrictInt, Field(gt=0)]
Quantity = Annotated[StrictInt, Field(gt=0)]
Identifier = Annotated[str, Field(min_length=1, max_length=120)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=200)]


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Fixed-width UTC ISO format so timestamps compare lexicographically."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_timestamp(value: str) -> datetime:
    """Parse both our fixed-width 'Z' format and Postgres '+00:00' timestamps."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_pence(pence: int | None) -> str:
    if pence is None:
        return "n/a"
    return f"£{pence // 100:,}.{pence % 100:02d}"


# --------------------------------------------------------------------------- enums


class PolicyCode(StrEnum):
    ALLOWED = "ALLOWED"
    KILL_SWITCH_ON = "KILL_SWITCH_ON"
    MERCHANT_NOT_ALLOWED = "MERCHANT_NOT_ALLOWED"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    BELOW_FLOOR = "BELOW_FLOOR"
    ABOVE_REQUEST_MAX = "ABOVE_REQUEST_MAX"
    PER_ORDER_CAP_EXCEEDED = "PER_ORDER_CAP_EXCEEDED"
    VELOCITY_LIMIT_EXCEEDED = "VELOCITY_LIMIT_EXCEEDED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    REFERENCE_PRICE_EXCEEDED = "REFERENCE_PRICE_EXCEEDED"
    MALFORMED_LLM_OUTPUT = "MALFORMED_LLM_OUTPUT"
    ROUND_LIMIT_REACHED = "ROUND_LIMIT_REACHED"
    NEGOTIATION_TIMEOUT = "NEGOTIATION_TIMEOUT"
    # Quote-validation and workflow codes.
    MERCHANT_TIMEOUT = "MERCHANT_TIMEOUT"
    MERCHANT_UNAVAILABLE = "MERCHANT_UNAVAILABLE"
    UNKNOWN_SKU = "UNKNOWN_SKU"
    OWNERSHIP_MISMATCH = "OWNERSHIP_MISMATCH"
    REQUEST_CLOSED = "REQUEST_CLOSED"
    NO_ACTIVE_MANDATE = "NO_ACTIVE_MANDATE"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    PAYMENT_FAILED = "PAYMENT_FAILED"


class RiskFlag(StrEnum):
    LOW_CONFIDENCE_REFERENCE = "LOW_CONFIDENCE_REFERENCE"
    SEEDED_REFERENCE = "SEEDED_REFERENCE"
    NO_REFERENCE = "NO_REFERENCE"
    ABOVE_REFERENCE = "ABOVE_REFERENCE"


class RequestStatus(StrEnum):
    REQUESTED = "requested"
    QUOTED = "quoted"
    NEGOTIATING = "negotiating"
    RESTING = "resting"
    RESERVED = "reserved"
    PAID = "paid"
    REJECTED = "rejected"
    OUT_OF_STOCK = "out_of_stock"
    CANCELLED = "cancelled"


OPEN_REQUEST_STATUSES = frozenset(
    {RequestStatus.REQUESTED, RequestStatus.QUOTED, RequestStatus.NEGOTIATING,
     RequestStatus.RESTING}
)


class QuoteStatus(StrEnum):
    PROPOSED = "proposed"
    VALID = "valid"
    COUNTERED = "countered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class QuoteOrigin(StrEnum):
    AGENT = "agent"
    REPRICE = "reprice"
    API = "api"


class OrderStatus(StrEnum):
    RESERVED = "reserved"
    PAID = "paid"
    PAYMENT_FAILED = "payment_failed"


class ReferenceConfidence(StrEnum):
    HIGH = "high"
    LOW = "low"


class ReferenceSource(StrEnum):
    TAVILY_LIVE = "tavily_live"
    TAVILY_CACHE = "tavily_cache"
    SEED_FALLBACK = "seed_fallback"


Stage = Literal["llm", "policy", "database", "system"]


# ------------------------------------------------------------------------ commands


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MandateCreate(Command):
    budget_pence: PositivePence
    max_per_order_pence: PositivePence
    allowed_merchant_ids: list[Identifier] = Field(min_length=1, max_length=50)
    orders_per_minute: Annotated[StrictInt, Field(gt=0, le=1000)]

    @model_validator(mode="after")
    def _cap_within_budget(self) -> MandateCreate:
        if self.max_per_order_pence > self.budget_pence:
            raise ValueError("max_per_order_pence cannot exceed budget_pence")
        return self


class KillCommand(Command):
    killed: StrictBool


class RFQCreate(Command):
    query: Annotated[str, Field(min_length=1, max_length=500)]
    category: Annotated[str, Field(min_length=1, max_length=80)]
    max_price_pence: PositivePence
    quantity: Quantity
    deadline: datetime

    @model_validator(mode="after")
    def _deadline_aware(self) -> RFQCreate:
        if self.deadline.tzinfo is None:
            self.deadline = self.deadline.replace(tzinfo=UTC)
        return self


class TextRFQ(Command):
    """Free-text buyer instruction; parsed by the LLM into an RFQ proposal."""

    text: Annotated[str, Field(min_length=1, max_length=2000)]


class QuoteCreate(Command):
    request_id: Identifier
    merchant_id: Identifier
    inventory_id: Identifier
    price_pence: PositivePence
    delivery_days: Annotated[StrictInt, Field(ge=0, le=365)]
    round: Annotated[StrictInt, Field(ge=1, le=3)] = 1


class CounterCreate(Command):
    price_pence: PositivePence


class ReserveCommand(Command):
    request_id: Identifier
    quote_id: Identifier
    idempotency_key: IdempotencyKey


class CheckoutCommand(Command):
    request_id: Identifier
    quote_id: Identifier
    idempotency_key: IdempotencyKey


class RepriceCommand(Command):
    inventory_id: Identifier
    price_pence: PositivePence


# ------------------------------------------------------------------------- records


class Record(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MerchantRecord(Record):
    id: str
    name: str
    rating: float
    created_at: str


class MandateRecord(Record):
    id: str
    budget_pence: int
    spent_pence: int
    max_per_order_pence: int
    allowed_merchant_ids: list[str]
    orders_per_minute: int
    killed: bool
    created_at: str
    updated_at: str

    @property
    def remaining_pence(self) -> int:
        return self.budget_pence - self.spent_pence


class InventoryRecord(Record):
    id: str
    sku: str
    merchant_id: str
    title: str
    category: str
    list_price_pence: int
    floor_price_pence: int
    stock: int
    delivery_days: int
    external_id: str | None = None
    external_price_pence: int | None = None


class RequestRecord(Record):
    id: str
    mandate_id: str
    query: str
    category: str
    max_price_pence: int
    quantity: int
    deadline: str
    status: RequestStatus
    round: int
    created_at: str
    updated_at: str


class QuoteRecord(Record):
    id: str
    request_id: str
    merchant_id: str
    inventory_id: str | None
    price_pence: int | None
    delivery_days: int | None
    round: int
    status: QuoteStatus
    origin: QuoteOrigin = QuoteOrigin.AGENT
    rejection_reason: str | None
    created_at: str


class OrderRecord(Record):
    id: str
    request_id: str
    quote_id: str
    mandate_id: str
    inventory_id: str
    idempotency_key: str
    quantity: int
    price_pence: int
    budget_committed: bool
    payment_reference: str | None = None
    status: OrderStatus
    created_at: str

    @property
    def total_pence(self) -> int:
        return self.price_pence * self.quantity


class EventCreate(Record):
    type: str
    stage: Stage
    request_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float | None = None


class EventRecord(EventCreate):
    id: str
    created_at: str


class ReferencePriceCreate(Record):
    query_key: str
    product_name: str
    country: str = "GB"
    currency: str = "GBP"
    price_pence: int
    source: ReferenceSource
    confidence: ReferenceConfidence
    source_urls: list[str] = Field(default_factory=list)
    fetched_at: str
    expires_at: str


class ReferencePriceRecord(ReferencePriceCreate):
    id: str


class ExecReportCreate(Record):
    order_id: str
    paid_pence: int
    best_quote_pence: int
    average_quote_pence: int
    saved_pence: int
    web_reference_pence: int | None
    reference_source: str | None
    reference_confidence: str | None
    reference_urls: list[str] = Field(default_factory=list)
    summary: str


class ExecReportRecord(ExecReportCreate):
    id: str
    created_at: str
    payment_simulated: bool = True


# ----------------------------------------------------------------------- decisions


class _AllowedAccessor:
    def __get__(self, instance: Any, owner: type) -> Any:
        if instance is None:
            return owner.allowed_decision
        return instance.code is PolicyCode.ALLOWED


class PolicyDecision(BaseModel):
    code: PolicyCode
    reason: str
    flags: list[RiskFlag] = Field(default_factory=list)

    # `PolicyDecision.allowed()` builds an allowed decision; `decision.allowed` is a bool.
    allowed: ClassVar[_AllowedAccessor]

    @classmethod
    def allowed_decision(cls, flags: list[RiskFlag] | None = None) -> PolicyDecision:
        from app.domain.policy import POLICY_REASONS

        return cls(code=PolicyCode.ALLOWED, reason=POLICY_REASONS[PolicyCode.ALLOWED],
                   flags=list(flags or []))

    @classmethod
    def reject(cls, code: PolicyCode, detail: str | None = None) -> PolicyDecision:
        from app.domain.policy import POLICY_REASONS

        reason = POLICY_REASONS[code] if detail is None else f"{POLICY_REASONS[code]} {detail}"
        return cls(code=code, reason=reason)


# ----------------------------------------------------------------------- responses


class PolicyRejected(Exception):
    """An expected policy failure; the API maps it to HTTP 422 with a stable code."""

    def __init__(self, decision: PolicyDecision, request_id: str | None = None) -> None:
        super().__init__(decision.code.value)
        self.decision = decision
        self.request_id = request_id


class PaymentResult(BaseModel):
    simulated: Literal[True] = True
    reference: str | None
    succeeded: bool
    note: str = "Simulated payment. No real money moved."


class ReservationResult(BaseModel):
    accepted: bool
    decision: PolicyDecision
    order: OrderRecord | None = None
    replayed: bool = False


class CheckoutResult(BaseModel):
    accepted: bool
    decision: PolicyDecision
    order: OrderRecord | None = None
    payment: PaymentResult | None = None
    report: ExecReportRecord | None = None
    replayed: bool = False


class RequestOutcome(BaseModel):
    request: RequestRecord
    quotes: list[QuoteRecord]
    events: list[EventRecord]
    decision: PolicyDecision | None = None
    order: OrderRecord | None = None
    payment: PaymentResult | None = None
    report: ExecReportRecord | None = None
    reference_price: ReferencePriceRecord | None = None

    @property
    def valid_quotes(self) -> list[QuoteRecord]:
        return [q for q in self.quotes if q.status is not QuoteStatus.REJECTED]


class RepriceOutcome(BaseModel):
    inventory: InventoryRecord
    filled_order_ids: list[str]
    skipped: list[dict[str, str]]


class StageLatency(BaseModel):
    count: int
    median_ms: float | None
    p95_ms: float | None


class DashboardCounters(BaseModel):
    orders: int
    blocked_attempts: int
    saved_pence: int


class DashboardSnapshot(BaseModel):
    profile: str
    payment_simulated: Literal[True] = True
    mandate: MandateRecord | None
    merchants: list[MerchantRecord]
    inventory: list[InventoryRecord]
    requests: list[RequestRecord]
    quotes: list[QuoteRecord]
    orders: list[OrderRecord]
    reports: list[ExecReportRecord]
    events: list[EventRecord]
    counters: DashboardCounters
    latency: dict[str, StageLatency]


def default_deadline() -> datetime:
    return utcnow() + timedelta(days=1)


PolicyDecision.allowed = _AllowedAccessor()  # type: ignore[misc]
