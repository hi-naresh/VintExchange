"""LLM boundary: typed proposals only.

LLM clients receive plain data and return strictly parsed proposal models. They
never receive a repository, database credentials, or any mutation capability.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from app.domain.models import (
    InventoryRecord,
    MerchantRecord,
    PolicyCode,
    QuoteRecord,
    RequestRecord,
)

MAX_RESPONSE_CHARS = 16_000


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RFQProposal(Proposal):
    query: Annotated[str, Field(min_length=1, max_length=500)]
    category: Annotated[str, Field(min_length=1, max_length=80)]
    max_price_pence: Annotated[StrictInt, Field(gt=0)]
    quantity: Annotated[StrictInt, Field(gt=0, le=10_000)] = 1
    deadline: datetime | None = Field(default=None, strict=False)


class QuoteProposal(Proposal):
    sku: Annotated[str, Field(min_length=1, max_length=80)]
    price_pence: Annotated[StrictInt, Field(gt=0)]
    delivery_days: Annotated[StrictInt, Field(ge=0, le=365)]
    note: Annotated[str, Field(max_length=280)] | None = None


class CounterProposal(Proposal):
    action: Literal["counter", "accept", "walk"]
    price_pence: Annotated[StrictInt, Field(gt=0)] | None = None


class LLMError(Exception):
    """Base class; messages never include credentials or raw prompts."""


class LLMOutputError(LLMError):
    def __init__(self, operation: str, detail: str) -> None:
        super().__init__(f"{operation}: malformed LLM output ({detail})")
        self.operation = operation
        self.code = PolicyCode.MALFORMED_LLM_OUTPUT


class LLMTransportError(LLMError):
    pass


class ReplayMiss(LLMError):
    pass



def parse_model[M: Proposal](model: type[M], text: str, operation: str) -> M:
    if not isinstance(text, str):
        raise LLMOutputError(operation, "response was not text")
    if len(text) > MAX_RESPONSE_CHARS:
        raise LLMOutputError(operation, "response too large")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise LLMOutputError(operation, "invalid JSON") from None
    if not isinstance(data, dict):
        raise LLMOutputError(operation, "expected a JSON object")
    try:
        return model.model_validate(data)
    except ValidationError as error:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "root" for e in error.errors()})
        raise LLMOutputError(operation, f"invalid fields: {', '.join(fields)}") from None


class LLMClient(Protocol):
    model_name: str

    async def parse_rfq(self, text: str) -> RFQProposal: ...

    async def quote(self, merchant: MerchantRecord, inventory: list[InventoryRecord],
                    request: RequestRecord, round: int,
                    counter_pence: int | None = None) -> QuoteProposal: ...

    async def counter(self, request: RequestRecord, quote: QuoteRecord,
                      round: int) -> CounterProposal: ...
