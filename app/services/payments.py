"""Simulated payment boundary.

The gateway is idempotent by key, mirroring how a processor such as Stripe
treats idempotency keys: repeating a charge with the same key returns the first
result and never charges twice. No real payment data is accepted or processed.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Protocol

from app.domain.models import PaymentResult


class PaymentGateway(Protocol):
    async def charge(self, idempotency_key: str, amount_pence: int) -> PaymentResult: ...


class SimulatedPaymentGateway:
    def __init__(self, fail_keys: set[str] | None = None) -> None:
        self.fail_keys = set(fail_keys or ())
        self._results: dict[str, PaymentResult] = {}
        self._lock = asyncio.Lock()
        self.charge_count = 0

    async def charge(self, idempotency_key: str, amount_pence: int) -> PaymentResult:
        async with self._lock:
            existing = self._results.get(idempotency_key)
            if existing is not None:
                return existing
            self.charge_count += 1
            digest = hashlib.sha256(f"{idempotency_key}:{amount_pence}".encode()).hexdigest()
            succeeded = idempotency_key not in self.fail_keys
            result = PaymentResult(reference=f"sim_{digest[:16]}" if succeeded else None,
                                   succeeded=succeeded)
            self._results[idempotency_key] = result
            return result
