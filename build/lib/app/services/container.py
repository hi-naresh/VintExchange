"""Service wiring shared by the API, scripts, and tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.protocol import LLMClient
from app.config import Settings
from app.market.protocol import MarketPriceProvider
from app.repositories.protocol import Repository
from app.services.commerce import CommerceService, OrderSink
from app.services.events import EventRecorder, EventSink
from app.services.orchestration import OrchestrationService
from app.services.payments import PaymentGateway, SimulatedPaymentGateway
from app.services.reports import ReportService
from app.services.repricing import RepricingService


@dataclass
class Services:
    settings: Settings
    repository: Repository
    llm: LLMClient
    market: MarketPriceProvider
    payment: PaymentGateway = field(default_factory=SimulatedPaymentGateway)
    order_sink: OrderSink | None = None
    event_sinks: list[EventSink] = field(default_factory=list)
    catalog_description: str = ""

    def __post_init__(self) -> None:
        self.recorder = EventRecorder(self.repository, self.event_sinks)
        self.commerce = CommerceService(self.repository, self.recorder, self.payment,
                                        self.market, self.order_sink)
        self.orchestrator = OrchestrationService(self.repository, self.llm, self.market,
                                                 self.commerce, self.recorder, self.settings)
        self.repricing = RepricingService(self.repository, self.commerce, self.recorder,
                                          self.market)
        self.reports = ReportService(self.repository)
