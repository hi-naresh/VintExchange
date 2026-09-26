# VintExchange Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reliable, three-minute agentic-commerce demo in which Grok proposes trades and deterministic application/database controls alone may execute them.

**Architecture:** FastAPI routes call focused orchestration and commerce services over a repository protocol. SQLite plus fake/replay LLMs form the zero-secret default; Supabase/Postgres and a real Grok adapter are connected-profile implementations of the same boundaries. Every state mutation is typed, policy-checked, transactional, and evented.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2 async, aiosqlite, httpx, pytest, pytest-asyncio, Supabase/Postgres SQL and Realtime, Tavily Search, Next.js/TypeScript generated with v0, supabase-js 2.x, Vercel, and Railway.

**Spec:** `docs/superpowers/specs/2026-09-26-vintexchange-design.md`

## Global Constraints

- Store all money as integer pence; never use floating point for prices.
- The LLM receives no repository/database capability and never mutates state.
- Default startup must require no API keys, network, Node, or frontend build step.
- Merchant calls run concurrently with an eight-second timeout each.
- Negotiation stops after three total rounds or 30 seconds; `NEGOTIATION_ENABLED=false` makes it single-round.
- SQLite and Postgres must guard stock, idempotency, and budget at the database transaction boundary.
- A high-confidence web reference must block fills above 150%; weak or fallback
  references warn without blocking.
- Tavily gets at most one call per canonical product request, runs concurrently,
  times out after three seconds, caches for 24 hours, and never blocks a fill.
- Payment is simulated and must be labeled as simulated in API data, UI, and README.
- Expected policy failures return stable reason codes and audit events.
- Real Grok responses pass through strict Pydantic parsing before domain services see them.
- Browser clients have select-only Supabase RLS access; all mutations go through
  the public HTTPS FastAPI service.
- Do not initialize or commit Git from this Just Chat workspace; each task ends with a verified checkpoint instead.

## File map

```text
app/config.py                         validated runtime settings
app/main.py                           application factory and static mount
app/api/dependencies.py               service/repository dependency access
app/api/routes.py                     HTTP/tool endpoints
app/domain/models.py                  commands, records, enums, responses
app/domain/policy.py                  pure deterministic decisions
app/repositories/protocol.py          persistence contract
app/repositories/sqlite.py            async SQLite implementation
app/repositories/supabase.py          connected REST/RPC implementation
app/services/events.py                timed stage/event recording
app/services/commerce.py              reservation and idempotent checkout
app/services/orchestration.py         RFQ fan-out and bounded negotiation
app/services/repricing.py             resting-order recheck
app/services/reports.py               execution report generation
app/market/protocol.py                 market-price provider boundary
app/market/fake.py                     deterministic reference provider
app/market/cache.py                    24-hour repository-backed cache
app/market/tavily.py                   bounded Tavily Search adapter
app/agents/protocol.py                LLM interface and strict result types
app/agents/fake.py                    scenario-driven deterministic client
app/agents/replay.py                  prompt-hash read/write wrapper
app/agents/grok.py                    xAI-compatible HTTP client
dashboard/                             v0-generated Next.js/TypeScript app
dashboard/lib/supabase.ts              read-only Realtime client
dashboard/lib/api.ts                   FastAPI mutation client
supabase/migrations/001_schema.sql     tables, constraints, transaction RPCs
supabase/migrations/002_realtime.sql   realtime publication
supabase/seed.sql                      three merchants and six SKUs
scripts/reset.py                       deterministic local reset
scripts/demo.py                        four scenario verifier
scripts/refresh_prices.py              explicit Tavily reference refresh
tests/conftest.py                      isolated app/repository fixtures
tests/unit/test_policy.py              policy codes
tests/unit/test_llm.py                 malformed/replay/timeout behavior
tests/unit/test_market_prices.py       parse/cache/timeout/overpay behavior
tests/invariant/test_transactions.py   race/idempotency/budget proofs
tests/integration/test_api.py          endpoint contracts
tests/integration/test_workflows.py    fill/rest/block/out-of-stock flows
dashboard/tests/dashboard.test.tsx     layout, state, mutation/realtime behavior
pyproject.toml                         dependencies and test configuration
.env.example                           connected-profile variables
.gitignore                             local database/cache/secrets
README.md                              setup, demo, architecture, honesty
```

---

### Task 1: Project skeleton, settings, and typed domain

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `app/domain/__init__.py`
- Create: `app/domain/models.py`
- Create: `tests/unit/test_models.py`

**Interfaces:**
- Produces: `Settings`, `MoneyPence`, `PolicyCode`, `RequestStatus`, `QuoteStatus`, `OrderStatus`, `ReferenceConfidence`, `MandateCreate`, `RFQCreate`, `QuoteCreate`, `CounterCreate`, `ReserveCommand`, `CheckoutCommand`, `RepriceCommand`, `ReferencePriceRecord`, and record/response models.

- [ ] **Step 1: Create dependency metadata and test configuration**

```toml
[project]
name = "vintexchange"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.32,<1",
  "pydantic>=2.9,<3",
  "pydantic-settings>=2.6,<3",
  "sqlalchemy[asyncio]>=2.0.36,<3",
  "aiosqlite>=0.20,<1",
  "httpx>=0.27,<1",
]

[project.optional-dependencies]
dev = ["pytest>=8.3,<9", "pytest-asyncio>=0.24,<1", "ruff>=0.8,<1"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

- [ ] **Step 2: Write failing validation tests**

```python
import pytest
from pydantic import ValidationError
from app.domain.models import MandateCreate, RFQCreate

def test_money_and_quantities_are_positive_integers():
    with pytest.raises(ValidationError):
        RFQCreate(query="headphones", category="audio", max_price_pence=150.5,
                  quantity=1, deadline="2026-09-27T17:00:00Z")
    with pytest.raises(ValidationError):
        RFQCreate(query="headphones", category="audio", max_price_pence=15000,
                  quantity=0, deadline="2026-09-27T17:00:00Z")

def test_spend_limit_cannot_exceed_budget():
    with pytest.raises(ValidationError):
        MandateCreate(budget_pence=30_000, max_per_order_pence=90_000,
                      allowed_merchant_ids=["merchant-1"], orders_per_minute=3)
```

- [ ] **Step 3: Run the tests and confirm RED**

Run: `python -m pytest tests/unit/test_models.py -v`

Expected: collection fails because `app.domain.models` does not exist.

- [ ] **Step 4: Implement strict models and environment settings**

Use `StrictInt` with `Field(ge=0)` for pence, `Field(gt=0)` for quantities,
`ConfigDict(extra="forbid")` on input models, string enums for statuses/reasons,
and an `after` validator on `MandateCreate` enforcing
`max_per_order_pence <= budget_pence`. `Settings` defaults to
`profile="offline"`, `database_url="sqlite+aiosqlite:///data/vintexchange.db"`,
`llm_mode="fake"`, `merchant_timeout_seconds=8`, `negotiation_timeout_seconds=30`,
and `max_rounds=3`. Connected profile validation requires Supabase URL/key and
Grok base URL/API key.

```python
MoneyPence = Annotated[StrictInt, Field(ge=0)]

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
```

- [ ] **Step 5: Verify GREEN and lint**

Run: `python -m pytest tests/unit/test_models.py -v && python -m ruff check app tests`

Expected: model tests pass; Ruff reports no errors.

---

### Task 2: SQLite schema, seed data, and repository boundary

**Files:**
- Create: `app/repositories/__init__.py`
- Create: `app/repositories/protocol.py`
- Create: `app/repositories/sqlite.py`
- Create: `app/repositories/schema.sql`
- Create: `app/repositories/seed.py`
- Create: `tests/conftest.py`
- Create: `tests/integration/test_repository.py`

**Interfaces:**
- Consumes: domain command/record models from Task 1.
- Produces: `Repository` protocol; `SQLiteRepository.connect()`, `.initialize()`, `.reset()`, `.close()`; CRUD/query methods named in the protocol; `seed_repository(repository)`.

- [ ] **Step 1: Define the repository contract**

```python
class Repository(Protocol):
    async def set_mandate(self, command: MandateCreate) -> MandateRecord: ...
    async def get_active_mandate(self) -> MandateRecord | None: ...
    async def set_killed(self, killed: bool) -> MandateRecord: ...
    async def list_inventory(self, category: str | None = None) -> list[InventoryRecord]: ...
    async def create_request(self, command: RFQCreate) -> RequestRecord: ...
    async def update_request(self, request_id: str, *, status: RequestStatus,
                             round: int | None = None) -> RequestRecord: ...
    async def create_quote(self, command: QuoteCreate, *, status: QuoteStatus,
                           rejection_reason: str | None) -> QuoteRecord: ...
    async def list_quotes(self, request_id: str) -> list[QuoteRecord]: ...
    async def get_request(self, request_id: str) -> RequestRecord | None: ...
    async def get_inventory_item(self, inventory_id: str) -> InventoryRecord | None: ...
    async def create_event(self, event: EventCreate) -> EventRecord: ...
    async def get_reference_price(self, query_key: str) -> ReferencePriceRecord | None: ...
    async def upsert_reference_price(self, record: ReferencePriceCreate) -> ReferencePriceRecord: ...
    async def dashboard_snapshot(self) -> DashboardSnapshot: ...
```

- [ ] **Step 2: Write failing persistence and seed tests**

```python
async def test_seed_has_three_merchants_and_six_skus(repository):
    snapshot = await repository.dashboard_snapshot()
    assert len(snapshot.merchants) == 3
    assert len(snapshot.inventory) == 6

async def test_quote_references_request_merchant_and_inventory(repository, seeded_request):
    with pytest.raises(RepositoryConflict):
        await repository.create_quote(
            QuoteCreate(request_id=seeded_request.id, merchant_id="unknown",
                        inventory_id="unknown", price_pence=14_800,
                        delivery_days=1, round=1),
            status=QuoteStatus.VALID,
            rejection_reason=None,
        )
```

- [ ] **Step 3: Run repository tests and confirm RED**

Run: `python -m pytest tests/integration/test_repository.py -v`

Expected: import failure for the missing repository implementation.

- [ ] **Step 4: Implement normalized schema and repository**

Create all nine specified tables, including `reference_prices`, foreign keys,
checks, indexes on request
status/category and event time, unique order idempotency key, unique order
request ID, and UTC ISO timestamps. Configure SQLite with WAL mode, foreign keys,
and a five-second busy timeout. Implement each protocol method using bound SQL
parameters and map rows into Pydantic records. The seed IDs must be stable:
`merchant-aurora`, `merchant-bassline`, `merchant-circuit`; six SKUs include the
headline items priced to quote £158, £162, and £166 initially, with the Bassline
floor at £148.

- [ ] **Step 5: Verify repository behavior**

Run: `python -m pytest tests/integration/test_repository.py -v`

Expected: seed count, constraints, CRUD, and dashboard snapshot tests pass.

---

### Task 3: Pure policy engine and event timing

**Files:**
- Create: `app/domain/policy.py`
- Create: `app/services/__init__.py`
- Create: `app/services/events.py`
- Create: `tests/unit/test_policy.py`
- Create: `tests/unit/test_events.py`

**Interfaces:**
- Consumes: `MandateRecord`, `InventoryRecord`, `RequestRecord`, `QuoteRecord`.
- Produces: `PolicyContext`, `PolicyDecision`, `evaluate_quote(context)`, `EventRecorder.measure(stage, event_type, request_id)`.

- [ ] **Step 1: Write one focused failing test for every policy code**

```python
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"mandate.killed": True}, PolicyCode.KILL_SWITCH_ON),
        ({"quote.merchant_id": "merchant-other"}, PolicyCode.MERCHANT_NOT_ALLOWED),
        ({"inventory.stock": 0}, PolicyCode.OUT_OF_STOCK),
        ({"quote.price_pence": 14_700}, PolicyCode.BELOW_FLOOR),
        ({"request.max_price_pence": 14_000}, PolicyCode.ABOVE_REQUEST_MAX),
        ({"mandate.max_per_order_pence": 14_000}, PolicyCode.PER_ORDER_CAP_EXCEEDED),
        ({"recent_orders": 3}, PolicyCode.VELOCITY_LIMIT_EXCEEDED),
        ({"mandate.spent_pence": 20_000}, PolicyCode.BUDGET_EXCEEDED),
        ({"quote.price_pence": 90_000, "reference.price_pence": 15_900,
          "reference.confidence": "high"}, PolicyCode.REFERENCE_PRICE_EXCEEDED),
    ],
)
def test_policy_rejects_with_stable_reason(policy_context, change, expected):
    context = replace_nested(policy_context, change)
    assert evaluate_quote(context).code is expected

def test_policy_allows_valid_quote(policy_context):
    assert evaluate_quote(policy_context) == PolicyDecision.allowed()
```

- [ ] **Step 2: Run policy tests and confirm RED**

Run: `python -m pytest tests/unit/test_policy.py -v`

Expected: import failure for `evaluate_quote`.

- [ ] **Step 3: Implement checks in the exact spec order**

`evaluate_quote` must be synchronous and side-effect-free. Compute total as
`quote.price_pence * request.quantity`. Return immediately at the first failed
check. Centralize reason copy in a `POLICY_REASONS: dict[PolicyCode, str]` so API,
events, and UI use identical wording. Apply the web-reference guard only when
confidence is `high`, comparing integer totals as `quote_total * 100 >
reference_total * 150`; weak and seeded fallback references add a risk flag to
the allowed decision rather than rejecting it.

- [ ] **Step 4: Add a failing event-timer test, then implement it**

```python
async def test_measure_records_stage_latency(repository):
    recorder = EventRecorder(repository)
    async with recorder.measure("policy", "policy_checked", "request-1"):
        await asyncio.sleep(0)
    events = (await repository.dashboard_snapshot()).events
    assert events[0].stage == "policy"
    assert events[0].latency_ms >= 0
```

Run first: `python -m pytest tests/unit/test_events.py -v`

Implement an `asynccontextmanager` using `time.monotonic_ns()`; record the event
in `finally` so failed stages also expose latency and sanitized error class.

- [ ] **Step 5: Verify policy and event suites**

Run: `python -m pytest tests/unit/test_policy.py tests/unit/test_events.py -v`

Expected: every stable-code case and timer test passes.

---

### Task 4: Atomic reservation, checkout, and reports

**Files:**
- Modify: `app/repositories/protocol.py`
- Modify: `app/repositories/sqlite.py`
- Create: `app/services/commerce.py`
- Create: `app/services/reports.py`
- Create: `tests/invariant/test_transactions.py`

**Interfaces:**
- Produces: `CommerceService.reserve(command) -> ReservationResult`, `CommerceService.checkout(command) -> CheckoutResult`, `CommerceService.execute(request_id, quote_id, idempotency_key) -> CheckoutResult`, `build_exec_report(order, quotes) -> ExecReportCreate`.
- Repository transaction methods: `reserve_atomic`, `checkout_atomic`, `mark_payment`, `release_failed_payment`, `get_order_by_idempotency_key`.

- [ ] **Step 1: Write the 20-way stock race test**

```python
async def test_twenty_reservations_for_one_unit_have_one_winner(service, one_stock_quote):
    results = await asyncio.gather(*[
        service.reserve(ReserveCommand(request_id=one_stock_quote.request_id,
                                       quote_id=one_stock_quote.id,
                                       idempotency_key=f"race-{i}"))
        for i in range(20)
    ])
    assert sum(result.accepted for result in results) == 1
    item = await service.repository.get_inventory_item(one_stock_quote.inventory_id)
    assert item.stock == 0
```

- [ ] **Step 2: Run the race test and confirm RED**

Run: `python -m pytest tests/invariant/test_transactions.py::test_twenty_reservations_for_one_unit_have_one_winner -v`

Expected: failure because `CommerceService` is missing.

- [ ] **Step 3: Implement the guarded reservation transaction**

Open a distinct SQLite connection per concurrent operation, issue
`BEGIN IMMEDIATE`, re-read current records, run `evaluate_quote`, execute guarded
stock and budget updates, create one `reserved` order, update request state, and
commit. Map no-row guards to `OUT_OF_STOCK` or `BUDGET_EXCEEDED`; retry SQLite
busy conflicts up to three times with `await asyncio.sleep(0.01 * attempt)`.

- [ ] **Step 4: Write and run concurrent idempotency and budget tests**

```python
async def test_same_checkout_key_creates_one_order(service, reservable_quote):
    commands = [CheckoutCommand(request_id=reservable_quote.request_id,
                                quote_id=reservable_quote.id,
                                idempotency_key="demo-key") for _ in range(20)]
    results = await asyncio.gather(*(service.checkout(c) for c in commands))
    assert len({result.order.id for result in results}) == 1
    assert await service.repository.count_orders() == 1

async def test_concurrent_orders_never_exceed_budget(service, two_requests_one_budget):
    results = await asyncio.gather(*[
        service.execute(request.id, request.quote_id, f"budget-{request.id}")
        for request in two_requests_one_budget
    ])
    mandate = await service.repository.get_active_mandate()
    assert sum(result.accepted for result in results) == 1
    assert mandate.spent_pence <= mandate.budget_pence
```

Run: `python -m pytest tests/invariant/test_transactions.py -v`

Expected before implementation: idempotency/budget tests fail on duplicate order
or missing methods.

- [ ] **Step 5: Implement idempotent checkout and report math**

`SimulatedPaymentGateway.charge` returns a deterministic transaction reference.
Checkout returns an existing order for a repeated key. On first success it marks
the order paid and writes exactly one report. Calculate average with integer
round-half-up and savings as `max(0, average_quote_pence - paid_pence)`. Failure
releases stock and budget once inside a guarded transaction.

- [ ] **Step 6: Verify all invariants repeatedly**

Run: `for i in 1 2 3 4 5; do python -m pytest tests/invariant/test_transactions.py -q || exit 1; done`

Expected: five clean runs, each proving one race winner, one idempotent order,
and no budget overspend.

---

### Task 5: LLM boundary, fake client, and replay cache

**Files:**
- Create: `app/agents/__init__.py`
- Create: `app/agents/protocol.py`
- Create: `app/agents/fake.py`
- Create: `app/agents/replay.py`
- Create: `app/agents/grok.py`
- Create: `tests/unit/test_llm.py`

**Interfaces:**
- Produces: `LLMClient.parse_rfq(text) -> RFQProposal`, `.quote(merchant, inventory, request, round) -> QuoteProposal`, `.counter(request, quote, round) -> CounterProposal`; `FakeLLMClient(script)`; `ReplayLLMClient(inner, cache_path, record, fallback)`; `GrokLLMClient(http_client, settings)`.

- [ ] **Step 1: Write strict-output, replay, and secret-safety tests**

```python
async def test_malformed_quote_becomes_typed_llm_error():
    client = FakeLLMClient({"quote": ['{"price_pence":"cheap"}']})
    with pytest.raises(LLMOutputError) as error:
        await client.quote(merchant(), inventory(), request(), 1)
    assert error.value.code is PolicyCode.MALFORMED_LLM_OUTPUT

async def test_replay_uses_normalized_prompt_hash(tmp_path):
    inner = CountingFakeLLM(valid_script())
    replay = ReplayLLMClient(inner, tmp_path / "cache.json", record=True)
    first = await replay.parse_rfq("  Headphones   under £150 ")
    second = await replay.parse_rfq("Headphones under £150")
    assert first == second
    assert inner.calls == 1
```

- [ ] **Step 2: Run LLM tests and confirm RED**

Run: `python -m pytest tests/unit/test_llm.py -v`

Expected: missing `app.agents.protocol`.

- [ ] **Step 3: Implement clients**

Normalize whitespace only; hash JSON containing operation, normalized prompt,
and model with SHA-256. Write caches atomically via a temporary file plus replace.
The real client POSTs OpenAI-compatible chat completions to the configured Grok
base URL, requests JSON output, limits response size, and validates only the
`message.content` JSON into the operation model. Redact authorization headers
and do not persist raw prompts in events.

- [ ] **Step 4: Verify LLM boundary**

Run: `python -m pytest tests/unit/test_llm.py -v`

Expected: malformed data fails cleanly, replay collapses normalized prompts, a
cache miss follows configured fallback, and serialized exceptions contain no key.

---

### Task 6: Tavily reference prices, cache, and overpay guard

**Files:**
- Create: `app/market/__init__.py`
- Create: `app/market/protocol.py`
- Create: `app/market/fake.py`
- Create: `app/market/cache.py`
- Create: `app/market/tavily.py`
- Modify: `app/repositories/protocol.py`
- Modify: `app/repositories/sqlite.py`
- Create: `tests/unit/test_market_prices.py`

**Interfaces:**
- Produces: `MarketPriceProvider.lookup(product_name, country="GB", currency="GBP") -> ReferencePrice`; `FakeMarketPriceProvider`; `CachedMarketPriceProvider`; `TavilyMarketPriceProvider`; `normalize_product_key`; `extract_gbp_prices`.
- Consumes: repository reference-price methods and an injected `httpx.AsyncClient`.

- [ ] **Step 1: Write failing deterministic extraction and confidence tests**

```python
def test_extracts_median_from_distinct_new_product_domains():
    results = [
        search_result("Shop A", "Sony WH-1000XM5 £159.00 new", "https://a.example/x"),
        search_result("Shop B", "Now £169.99", "https://b.example/y"),
        search_result("Used", "Refurbished £89", "https://c.example/z"),
    ]
    reference = parse_reference("Sony WH-1000XM5", results)
    assert reference.price_pence == 16_450
    assert reference.confidence is ReferenceConfidence.HIGH
    assert len(reference.source_urls) == 2

def test_single_source_is_low_confidence_and_non_blocking(policy_context):
    context = replace(policy_context, reference=reference(15_900, confidence="low"),
                      quote=quote(price_pence=90_000))
    decision = evaluate_quote(context)
    assert decision.allowed
    assert "LOW_CONFIDENCE_REFERENCE" in decision.flags
```

- [ ] **Step 2: Run extraction tests and confirm RED**

Run: `python -m pytest tests/unit/test_market_prices.py -v`

Expected: missing `app.market` package.

- [ ] **Step 3: Implement bounded Tavily lookup and parsing**

POST to `https://api.tavily.com/search` with `search_depth="fast"`,
`max_results=5`, `topic="general"`, `country="united kingdom"`, and
`include_answer=false`. Wrap the call in `asyncio.timeout(3)`. Extract prices
with a GBP regex from title/content, reject text containing `used`, `refurbished`,
`renewed`, `monthly`, or `per month`, keep one plausible price per registrable
domain, and use integer-pence median with half-up rounding. Two distinct domains
produce high confidence; fewer produce low confidence. Persist URLs, source, and
timestamps but never raw response bodies.

- [ ] **Step 4: Write failing timeout/cache tests, then implement cache fallback**

```python
async def test_timeout_returns_seed_without_waiting_beyond_three_seconds(clock, repository):
    provider = CachedMarketPriceProvider(
        TavilyMarketPriceProvider(hanging_http_client()), repository,
        timeout_seconds=0.03,
    )
    reference = await provider.lookup("Sony WH-1000XM5")
    assert reference.source == "seed_fallback"
    assert reference.price_pence == 15_900

async def test_unexpired_cache_avoids_second_network_call(repository, counting_tavily):
    provider = CachedMarketPriceProvider(counting_tavily, repository, ttl_hours=24)
    await provider.lookup("Sony WH-1000XM5")
    await provider.lookup("sony wh-1000xm5")
    assert counting_tavily.calls == 1
```

Run the two tests before implementation and confirm missing fallback/cache logic;
then implement normalized keys, 24-hour TTL, and seeded references.

- [ ] **Step 5: Verify market-price and policy suites**

Run: `python -m pytest tests/unit/test_market_prices.py tests/unit/test_policy.py -v`

Expected: parsing, domain de-duplication, cache, timeout fallback, high-confidence
hard block, and low-confidence warning tests pass.

---

### Task 7: Concurrent merchant quoting and bounded negotiation

**Files:**
- Create: `app/services/orchestration.py`
- Create: `tests/integration/test_workflows.py`

**Interfaces:**
- Consumes: repository, `LLMClient`, `MarketPriceProvider`, `CommerceService`, `EventRecorder`, settings.
- Produces: `OrchestrationService.submit_rfq(command) -> RequestOutcome`, `.submit_text(text) -> RequestOutcome`, `.counter_quote(quote_id, command) -> QuoteRecord`.

- [ ] **Step 1: Write failing concurrency and termination tests**

```python
async def test_three_merchants_quote_concurrently(orchestrator, timed_fake):
    started = time.monotonic()
    outcome = await orchestrator.submit_rfq(headphone_rfq())
    assert time.monotonic() - started < 0.20
    assert len(outcome.quotes) == 3

async def test_endless_counters_stop_at_three_rounds(orchestrator, endless_fake):
    outcome = await orchestrator.submit_rfq(headphone_rfq())
    assert outcome.request.round == 3
    assert endless_fake.counter_calls <= 6
    assert any(e.type == "negotiation_stopped" for e in outcome.events)

async def test_one_slow_merchant_does_not_cancel_fast_quotes(orchestrator, slow_fake):
    outcome = await orchestrator.submit_rfq(headphone_rfq())
    assert len(outcome.valid_quotes) == 2
    assert any(q.rejection_reason == "MERCHANT_TIMEOUT" for q in outcome.quotes)

async def test_market_lookup_runs_once_in_parallel_with_quotes(orchestrator, market_fake):
    outcome = await orchestrator.submit_rfq(headphone_rfq())
    assert market_fake.calls == 1
    assert outcome.reference_price.price_pence == 15_900
```

Configure test timeouts to 50ms and fake calls to 75ms; avoid an eight-second
test. Confirm RED with `python -m pytest tests/integration/test_workflows.py -v`.

- [ ] **Step 2: Implement fan-out, validation, ranking, and caps**

Use `asyncio.gather(*(one_merchant(...)), return_exceptions=True)` where each
`one_merchant` wraps its call in `asyncio.timeout(merchant_timeout_seconds)`.
Start exactly one `MarketPriceProvider.lookup` task before gathering merchant
quotes; cancel it at its own deadline and immediately use cache/seed fallback.
Validate ownership, SKU, stock, and floor before persistence. Rank by price,
delivery days, then descending merchant rating. Track a monotonic global
deadline; check it before every round and cap persisted request rounds at three.

- [ ] **Step 3: Implement the cut-line flag**

When `negotiation_enabled` is false, skip counters after round one, rank valid
quotes, and either execute or rest. Add a test asserting zero counter calls and
all mandate/reservation behavior remains active.

- [ ] **Step 4: Verify orchestration and invariant suites together**

Run: `python -m pytest tests/integration/test_workflows.py tests/invariant/test_transactions.py -v`

Expected: concurrent quotes, isolated timeout, three-round termination,
single-round fallback, stock, budget, and idempotency all pass.

---

### Task 8: Repricing, limit fills, and execution reports

**Files:**
- Create: `app/services/repricing.py`
- Modify: `app/services/reports.py`
- Modify: `app/repositories/protocol.py`
- Modify: `app/repositories/sqlite.py`
- Modify: `tests/integration/test_workflows.py`

**Interfaces:**
- Produces: `RepricingService.reprice(merchant_id, command) -> RepriceOutcome`; repository `list_resting_requests(category)`; report retrieval by order ID.

- [ ] **Step 1: Write the rest-then-fill test**

```python
async def test_flash_sale_fills_oldest_matching_limit_order(app_services):
    outcome = await app_services.orchestrator.submit_rfq(headphone_rfq(max_pence=15_000))
    assert outcome.request.status is RequestStatus.RESTING
    result = await app_services.repricing.reprice(
        "merchant-bassline",
        RepriceCommand(inventory_id="inventory-bassline-pro", price_pence=14_800),
    )
    assert len(result.filled_order_ids) == 1
    report = await app_services.reports.get(result.filled_order_ids[0])
    assert report.paid_pence == 14_800
    assert report.average_quote_pence == 16_200
    assert report.saved_pence == 1_400
    assert report.web_reference_pence == 15_900
    assert report.reference_source in {"tavily_live", "tavily_cache", "seed_fallback"}
```

- [ ] **Step 2: Run the workflow test and confirm RED**

Run: `python -m pytest tests/integration/test_workflows.py::test_flash_sale_fills_oldest_matching_limit_order -v`

Expected: missing `RepricingService`.

- [ ] **Step 3: Implement validated reprice and oldest-first recheck**

Reject merchant mismatch and prices below floor. Commit price before querying
resting requests. For each request, create a system quote and call
`CommerceService.execute` with key `reprice:{request_id}:{inventory_id}:{price}`.
Continue after conflicts and event every fill/skip with the stable policy code.

- [ ] **Step 4: Verify all headline workflows**

Run: `python -m pytest tests/integration/test_workflows.py -v`

Expected: normal fill, rest-then-fill with £14 saved, adversarial block,
out-of-stock, below-floor reprice, and reference-price provenance all pass.

---

### Task 9: FastAPI tool endpoints and dashboard snapshot API

**Files:**
- Create: `app/api/__init__.py`
- Create: `app/api/dependencies.py`
- Create: `app/api/routes.py`
- Create: `app/main.py`
- Create: `tests/integration/test_api.py`

**Interfaces:**
- Produces all specified endpoints plus `GET /dashboard`, `GET /reference-prices/{query_key}`, and `GET /health`; `create_app(settings=None, repository=None, llm=None, market=None) -> FastAPI`.

- [ ] **Step 1: Write failing API contract tests**

```python
async def test_kill_switch_blocks_request_with_typed_reason(client):
    await client.post("/mandate", json=mandate_json())
    await client.post("/kill", json={"killed": True})
    response = await client.post("/requests", json=headphone_rfq_json())
    assert response.status_code == 422
    assert response.json()["reason_code"] == "KILL_SWITCH_ON"

async def test_checkout_is_idempotent_over_http(client, reservable_ids):
    payload = {**reservable_ids, "idempotency_key": "http-demo-key"}
    first = await client.post("/checkout", json=payload)
    second = await client.post("/checkout", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["order"]["id"] == second.json()["order"]["id"]
    assert first.json()["payment"]["simulated"] is True
```

- [ ] **Step 2: Run API tests and confirm RED**

Run: `python -m pytest tests/integration/test_api.py -v`

Expected: missing application factory/routes.

- [ ] **Step 3: Implement application lifespan and routes**

Initialize/close owned dependencies in FastAPI lifespan, but do not close injected
test dependencies. Convert `PolicyRejected` to a 422 JSON body with
`reason_code`, `reason`, and `request_id`; `NotFound` to 404; transaction conflict
to 409. Route bodies use Task 1 models directly. Configure explicit CORS origins
from settings for the Vercel production URL and local dashboard development;
never use wildcard origins with credentials.

- [ ] **Step 4: Verify endpoint matrix and OpenAPI**

Run: `python -m pytest tests/integration/test_api.py -v`

Expected: every specified method/path appears in `/openapi.json`, expected
failures have stable status/body, and payment response says simulated.

---

### Task 10: Supabase/Postgres connected profile

**Files:**
- Create: `supabase/migrations/001_schema.sql`
- Create: `supabase/migrations/002_realtime.sql`
- Create: `supabase/seed.sql`
- Create: `app/repositories/supabase.py`
- Create: `tests/unit/test_supabase_contract.py`

**Interfaces:**
- Produces: `SupabaseRepository(AsyncClient, Settings)` satisfying `Repository`; Postgres functions `reserve_atomic(...)`, `checkout_atomic(...)`, `release_failed_payment(...)`, and `reset_demo()`.

- [ ] **Step 1: Write a contract coverage test**

```python
def test_supabase_repository_implements_every_protocol_method():
    required = {
        name for name, value in Repository.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert required <= set(dir(SupabaseRepository))

def test_sql_contains_database_guards():
    sql = Path("supabase/migrations/001_schema.sql").read_text()
    assert "stock >= p_quantity" in sql
    assert "spent_pence + p_total <= budget_pence" in sql
    assert "unique (idempotency_key)" in sql.lower()
```

- [ ] **Step 2: Run contract tests and confirm RED**

Run: `python -m pytest tests/unit/test_supabase_contract.py -v`

Expected: missing SQL and adapter.

- [ ] **Step 3: Implement schema, RPC transactions, seed, and adapter**

Mirror the SQLite columns/checks/indexes with UUID/text IDs as seeded, use
`FOR UPDATE` inside security-invoker PL/pgSQL functions, and grant execution only
to the service role. Add all tables used by the board to `supabase_realtime` in
the second migration. The HTTP adapter calls table REST endpoints for ordinary
CRUD and RPC endpoints for protected mutations; it never emulates a transaction
with multiple independent REST writes.

- [ ] **Step 4: Verify connected artifacts without credentials**

Run: `python -m pytest tests/unit/test_supabase_contract.py -v`

Expected: protocol coverage, table presence, guarded SQL, realtime publication,
and stable seed IDs pass through static/adapter tests.

---

### Task 11: v0-generated three-panel exchange dashboard

**Files:**
- Create: `app/static/index.html`
- Create: `app/static/styles.css`
- Create: `app/static/app.js`
- Create: `tests/static/test_dashboard.py`

**Interfaces:**
- Consumes: `GET /dashboard`, `POST /mandate`, `POST /kill`, `POST /requests`, `POST /merchants/{id}/reprice`.
- Produces: accessible mandate form, kill switch, quote tape/state rail, flash-sale action, event feed, metrics, and latency comparison.

- [ ] **Step 1: Write failing semantic/static tests**

```python
def test_dashboard_has_three_named_regions():
    html = Path("app/static/index.html").read_text()
    soup = BeautifulSoup(html, "html.parser")
    assert soup.select_one('[aria-label="Mandate controls"]')
    assert soup.select_one('[aria-label="Request and quote market"]')
    assert soup.select_one('[aria-label="Risk and event feed"]')

def test_dashboard_supports_offline_and_realtime_sources():
    js = Path("app/static/app.js").read_text()
    assert 'fetch("/dashboard")' in js
    assert ".channel(" in js
    assert "setInterval" in js
```

Add `beautifulsoup4>=4.12,<5` to dev dependencies. Run
`python -m pytest tests/static/test_dashboard.py -v`; expect missing asset files.

- [ ] **Step 2: Implement the reviewed visual system**

Use the six approved colors as CSS custom properties; a three-column grid of
`minmax(15rem, .8fr) minmax(28rem, 2fr) minmax(18rem, 1fr)`; no rounded floating
card kit; table/tape rows with visible state rails. Use sentence-case labels,
visible `:focus-visible`, minimum 44px action targets, and a 900px media query
that stacks panels mandate/tape/feed. Respect `prefers-reduced-motion`.
Load a pinned Supabase JS 2.x CDN URL only when connected-profile bootstrap data
is present; the offline profile must not attempt that network request.

- [ ] **Step 3: Implement behavior and honest states**

Read bootstrap JSON to select polling or Supabase Realtime. Render state changes
without `innerHTML` for event payloads; use `textContent`. The kill switch asks
for confirmation only when turning on and remains a native checkbox/button pair.
Display “Simulated payment” beside paid states and report. Flash sale posts
£14800 to the Bassline SKU. Network errors retain the last snapshot and display
a specific disconnected banner.

- [ ] **Step 4: Verify static tests and inspect a browser screenshot**

Run: `python -m pytest tests/static/test_dashboard.py -v`

Start: `uvicorn app.main:app --port 8000`

Inspect desktop at 1440×900 and mobile at 390×844. Confirm no horizontal
overflow, readable state rails, keyboard focus, event colors with text/icons,
and one state-change highlight only.

---

### Task 12: Reset command, four-scenario demo, price refresh, and README

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/reset.py`
- Create: `scripts/demo.py`
- Create: `tests/integration/test_demo_script.py`
- Create: `README.md`

**Interfaces:**
- Produces: `python -m scripts.reset`; `python -m scripts.demo` with four PASS/FAIL lines and process exit status.

- [ ] **Step 1: Write the failing demo-script test**

```python
def test_demo_reports_all_four_scenarios(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.demo", "--database", str(tmp_path / "demo.db")],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-4:] == [
        "PASS normal fill",
        "PASS rest then fill",
        "PASS adversarial block",
        "PASS out of stock",
    ]
```

- [ ] **Step 2: Run the script test and confirm RED**

Run: `python -m pytest tests/integration/test_demo_script.py -v`

Expected: module `scripts.demo` is missing.

- [ ] **Step 3: Implement reset and scenario runner**

Reset deletes/recreates only the explicit configured demo database after
validating it is a `.db` file below the project `data/` directory or a path
passed by the test. Demo constructs an in-process offline application, runs each
scenario from a fresh reset, catches assertion errors to print FAIL plus reason,
prints the four lines in fixed order, and exits 1 if any failed.

- [ ] **Step 4: Write the operator documentation**

README sections: pitch, architecture and “Grok proposes; code disposes,” quick
start, offline/connected profiles, environment table, API/tool examples, tests,
three-minute demo script with timestamps, cut-line flag, replay recording/use,
reset/rehearsal checklist, Supabase setup, real Grok setup, simulated-payment
disclosure, security limitations, and the two prepared judge answers.

- [ ] **Step 5: Verify scripts and commands from README**

Run: `python -m scripts.reset && python -m scripts.demo`

Expected: database is seeded and all four exact PASS lines print.

---

### Task 13: Full verification and demo rehearsal

**Files:**
- Modify only files implicated by a failing test or rehearsal defect; every bug fix begins with a reproducing test.

**Interfaces:**
- Produces: fresh evidence for correctness, lint, application startup, API health, dashboard rendering, and demo timing.

- [ ] **Step 1: Run the complete automated suite**

Run: `python -m pytest -q`

Expected: zero failed, zero errors.

- [ ] **Step 2: Run lint**

Run: `python -m ruff check app scripts tests`

Expected: exit 0 and no diagnostics.

- [ ] **Step 3: Run the reliability proofs with explicit output**

Run: `python -m pytest tests/invariant/test_transactions.py -v`

Expected test names visibly prove 20 contenders/one unit/one success, one key/one
order, and concurrent spend at or below budget.

- [ ] **Step 4: Run the demo and health smoke test**

Run in terminal A: `uvicorn app.main:app --host 127.0.0.1 --port 8000`

Run in terminal B: `curl --fail http://127.0.0.1:8000/health && python -m scripts.demo`

Expected: health reports offline/fake dependencies ready; demo prints four PASS
lines; homepage and assets return 200.

- [ ] **Step 5: Rehearse the three-minute flow three times**

Before each run, execute `python -m scripts.reset`. Perform mandate set,
headphones RFQ, visible rest, Bassline flash sale, instant fill, adversarial £900
prompt block, exec report, and invariant-test reveal. Record elapsed times and
fix only defects that block the acceptance criteria. Create the backup video and
verify playback outside the browser before stage use.

- [ ] **Step 6: Re-read acceptance criteria and report evidence**

Map each of the nine spec acceptance criteria to a passing test, command output,
or inspected dashboard behavior. Report any connected-profile step not exercised
against live credentials as “implemented and statically verified, not live-tested.”
