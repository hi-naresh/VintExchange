import asyncio

import pytest

from app.services.events import EventRecorder


async def test_measure_records_stage_latency(repository):
    recorder = EventRecorder(repository)
    async with recorder.measure("policy", "policy_checked", "request-1"):
        await asyncio.sleep(0)
    events = (await repository.dashboard_snapshot()).events
    assert events[0].stage == "policy"
    assert events[0].latency_ms >= 0


async def test_failed_stage_still_records_latency_and_error_class(repository):
    recorder = EventRecorder(repository)
    with pytest.raises(RuntimeError):
        async with recorder.measure("llm", "merchant_quote", "request-1"):
            raise RuntimeError("secret detail sk-123")
    event = (await repository.dashboard_snapshot()).events[0]
    assert event.payload == {"error": "RuntimeError"}
    assert event.latency_ms is not None


async def test_secret_like_payload_keys_are_dropped(repository):
    recorder = EventRecorder(repository)
    event = await recorder.emit("x", api_key="sk-1", prompt="raw prompt", sku="BSL-PRO")
    assert event.payload == {"sku": "BSL-PRO"}


async def test_dashboard_latency_aggregates_per_stage(repository):
    recorder = EventRecorder(repository)
    for ms in (1.0, 2.0, 3.0, 4.0, 100.0):
        await recorder.emit("llm_call", stage="llm", latency_ms=ms)
    latency = (await repository.dashboard_snapshot()).latency
    assert latency["llm"].count == 5
    assert latency["llm"].median_ms == 3.0
    assert latency["llm"].p95_ms == 100.0
    assert latency["database"].count == 0
