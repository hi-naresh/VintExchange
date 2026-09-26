"""Execution-quality report math (integer pence, round-half-up)."""

from __future__ import annotations

from app.domain.models import (
    ExecReportCreate,
    ExecReportRecord,
    OrderRecord,
    QuoteOrigin,
    QuoteRecord,
    QuoteStatus,
    ReferencePriceRecord,
    ReferenceSource,
    format_pence,
)
from app.repositories.protocol import NotFound, Repository


def mean_half_up(values: list[int]) -> int:
    if not values:
        raise ValueError("mean of empty list")
    return (2 * sum(values) + len(values)) // (2 * len(values))


def competing_quote_totals(order: OrderRecord, quotes: list[QuoteRecord]) -> list[int]:
    """Each merchant's latest priced agent quote, as totals for the order quantity.

    System quotes created by a reprice are excluded: the report compares the fill
    against what the market quoted during negotiation.
    """
    priced = [q for q in quotes if q.status is not QuoteStatus.REJECTED
              and q.price_pence is not None]
    agent = [q for q in priced if q.origin is not QuoteOrigin.REPRICE] or priced
    latest: dict[str, QuoteRecord] = {}
    for quote in agent:
        current = latest.get(quote.merchant_id)
        if current is None or (quote.round, quote.created_at) >= (current.round,
                                                                   current.created_at):
            latest[quote.merchant_id] = quote
    return [q.price_pence * order.quantity for q in latest.values()]  # type: ignore[operator]


def build_exec_report(order: OrderRecord, quotes: list[QuoteRecord],
                      reference: ReferencePriceRecord | None) -> ExecReportCreate:
    paid = order.total_pence
    totals = competing_quote_totals(order, quotes) or [paid]
    best = min(totals)
    average = mean_half_up(totals)
    saved = max(0, average - paid)
    web_reference = reference.price_pence * order.quantity if reference else None

    parts = [f"Paid {format_pence(paid)} against a {format_pence(average)} average quote "
             f"(best negotiated {format_pence(best)}); saved {format_pence(saved)}."]
    if reference is None:
        parts.append("No web reference price was available.")
    else:
        provenance = f"{reference.source.value}, {reference.confidence.value} confidence"
        if reference.source is ReferenceSource.SEED_FALLBACK:
            provenance += ", deterministic demo data"
        parts.append(f"Web reference {format_pence(web_reference)} ({provenance}).")
    parts.append("Payment was simulated; no real money moved.")

    return ExecReportCreate(
        order_id=order.id, paid_pence=paid, best_quote_pence=best,
        average_quote_pence=average, saved_pence=saved, web_reference_pence=web_reference,
        reference_source=reference.source.value if reference else None,
        reference_confidence=reference.confidence.value if reference else None,
        reference_urls=list(reference.source_urls) if reference else [],
        summary=" ".join(parts),
    )


class ReportService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def get(self, order_id: str) -> ExecReportRecord:
        report = await self.repository.get_report(order_id)
        if report is None:
            raise NotFound("report", order_id)
        return report
