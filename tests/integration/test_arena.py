from unittest.mock import AsyncMock

from app.agents.fake import FakeLLMClient
from app.config import Settings
from app.market.fake import FakeMarketPriceProvider
from app.services.container import Services
from app.services.simulation import run_arena
from scripts.traffic import Market


async def test_arena_executes_isolated_and_repeats(repository):
    settings = Settings(_env_file=None, profile="offline", llm_mode="fake")
    sink = AsyncMock()
    services = Services(settings, repository, FakeLLMClient(), FakeMarketPriceProvider(),
                        order_sink=sink, event_sinks=[sink])
    before = (await repository.dashboard_snapshot()).model_dump()
    first = await run_arena(services)
    second = await run_arena(services)
    assert first["mode"] == "deterministic"
    assert first["filled"] >= 3
    assert first["blocked"] >= 1
    assert first["budget_pence"] == 200_000
    assert first["gmvolume_pence"] == first["spent_pence"]
    assert all(first["invariants"].values())
    assert first["gmvolume_pence"] == second["gmvolume_pence"]
    assert first["fees_pence"] == sum(m["fee_pence"] for m in first["merchants"])
    assert first["units_traded"] == sum(b["units"] for b in first["buyers"])
    assert first["timeline"]
    assert (await repository.dashboard_snapshot()).model_dump() == before
    assert not sink.mock_calls


def test_traffic_never_replenishes_without_opt_in():
    import random
    from unittest.mock import Mock
    market = Market("http://localhost", random.Random(1), 2000, 1200)
    market.open_mandate = Mock()
    market.snapshot = {"mandate": {"killed": False, "budget_pence": 200000,
                                   "spent_pence": 199999}}
    market.ensure_budget()
    market.open_mandate.assert_not_called()
    market.replenish_budget = True
    market.ensure_budget()
    market.open_mandate.assert_called_once()
    market.open_mandate.reset_mock()
    market.snapshot["mandate"]["killed"] = True
    market.ensure_budget()
    market.open_mandate.assert_not_called()
    market.http.close()


async def test_arena_http_contract_and_live_mandate_untouched(repository):
    import httpx

    from app.main import create_app

    await repository.set_killed(True)
    before = (await repository.get_active_mandate()).model_dump()
    app = create_app(
        settings=Settings(_env_file=None, profile="offline", llm_mode="fake"),
        repository=repository, llm=FakeLLMClient(), market=FakeMarketPriceProvider(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://test") as client:
        response = await client.post("/simulate/arena", json={})
        assert response.status_code == 200
        result = response.json()
        assert result["payments"] == "simulated"
        assert result["mode"] == "deterministic"
        assert result["filled"] == 4
        assert result["blocked"] == 1
        assert result["gmvolume_pence"] == 197600
        assert result["savings_pence"] == 10600
        assert result["fees_pence"] == 2964
        assert result["remaining_pence"] == 2400
        assert result["buyers"][-1]["code"] == "PER_ORDER_CAP_EXCEEDED"
        assert all(result["invariants"].values())
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/static/arena.js")).status_code == 200
    assert (await repository.get_active_mandate()).model_dump() == before
