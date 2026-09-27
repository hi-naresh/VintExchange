"""xAI-compatible chat-completions client.

Only `choices[0].message.content` is used, and it must parse into the strict
operation model. Errors never include the Authorization header, the API key, or
the raw prompt.
"""

from __future__ import annotations

import httpx

from app.agents.prompts import counter_messages, quote_messages, rfq_messages
from app.agents.protocol import (
    CounterProposal,
    LLMOutputError,
    LLMTransportError,
    Proposal,
    QuoteProposal,
    RFQProposal,
    parse_model,
)
from app.config import Settings
from app.domain.models import InventoryRecord, MerchantRecord, QuoteRecord, RequestRecord

MAX_BODY_BYTES = 64_000


class GrokLLMClient:
    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http_client
        self._url = settings.grok_base_url.rstrip("/") + "/chat/completions"
        self._key = settings.grok_api_key
        self.model_name = settings.grok_model

    def __repr__(self) -> str:
        return f"GrokLLMClient(model={self.model_name!r}, url={self._url!r})"

    async def _complete(self, operation: str, messages: tuple[str, str],
                        model: type[Proposal]) -> Proposal:
        body = {
            "model": self.model_name,
            "messages": [{"role": "system", "content": messages[0]},
                         {"role": "user", "content": messages[1]}],
            "temperature": 0,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        try:
            response = await self._http.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise LLMTransportError(f"{operation}: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise LLMTransportError(f"{operation}: HTTP {response.status_code}")
        if len(response.content) > MAX_BODY_BYTES:
            raise LLMOutputError(operation, "response too large")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMOutputError(operation, "unexpected completion envelope") from None
        return parse_model(model, content, operation)

    async def parse_rfq(self, text: str) -> RFQProposal:
        result = await self._complete("parse_rfq", rfq_messages(text), RFQProposal)
        assert isinstance(result, RFQProposal)
        return result

    async def quote(self, merchant: MerchantRecord, inventory: list[InventoryRecord],
                    request: RequestRecord, round: int,
                    counter_pence: int | None = None) -> QuoteProposal:
        messages = quote_messages(merchant, inventory, request, round, counter_pence)
        result = await self._complete("quote", messages, QuoteProposal)
        assert isinstance(result, QuoteProposal)
        return result

    async def counter(self, request: RequestRecord, quote: QuoteRecord,
                      round: int) -> CounterProposal:
        result = await self._complete("counter", counter_messages(request, quote, round),
                                      CounterProposal)
        assert isinstance(result, CounterProposal)
        return result
