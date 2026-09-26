# Vint Exchange

**Start with the Agent arena:** one click runs a fresh isolated market, displays buyer and seller outcomes, and proves budget and stock accounting. [Judge walkthrough and limitations](docs/submission/README.md).

`POST /simulate/arena` uses the configured agent mode with seeded stock, a fixed £2,000 shared mandate, simulated payment, and no external order writes. The default simulation fills 95 units for £1,976, saves £106 against quotes, and leaves £24.

**Track: Agentic Commerce.** Demo theme: wholesale secondhand clothing lots, the
Fleek model: a buyer asks for "50 grade-A vintage denim jackets under £18 each" and
supplier agents compete for the lot.

**Business model:** merchants pay a take rate per fill, like an exchange fee
(1.5% by default, `TAKE_RATE_BPS`). No fill, no fee. Buyers get agents they can
trust with a budget; suppliers see live demand at a price and clear stock without
blind discounting.

**Any agent can trade here:** a Grok Bot teammate can place orders through the
public API (see `docs/grok-bot-buyer.md`); the mandate still decides.

 Grok agents shop real Shopify catalogues for you,
negotiate with merchant agents, and buy — inside a human mandate that no prompt
can break.

A three-minute agentic-commerce demo built on one rule:

> **Grok proposes; deterministic code disposes.**

Buyer and merchant agents suggest requests, quotes, and counter-offers. Only typed
application code may check policy, move stock, commit budget, or create an order.

**Payment is always simulated.** No card data is accepted, and no real money moves.
The payment boundary (`app/services/payments.py`) is an interface that a processor
such as Stripe could back later.

The demo proves four things:

1. Several merchant agents quote at the same time and negotiate.
2. A request that can't fill straight away rests, then fills after a reprice.
3. A prompt injection can't get round the human's mandate.
4. Concurrency guards and idempotency stop oversells and duplicate charges.

## Commerce stack

| Tool | How it is used |
|---|---|
| **Shopify Storefront API** | `CATALOG_SOURCE=shopify` loads merchants (product vendor), SKUs, GBP prices and stock into the exchange on reset |
| **Shopify Admin API** | Optional `SHOPIFY_ADMIN_TOKEN`: every fill is mirrored as a Shopify **draft order** with the negotiated discount, tagged `simulated-payment` |
| **PostHog** | Every exchange event (`gx_request_created`, `gx_quote_received`, `gx_policy_rejected`, `gx_order_paid`, …) is forwarded for funnels: request → quote → fill vs blocked |
| **Grok (xAI)** | Buyer parsing, merchant quoting, and buyer counters — proposals only |
| **Tavily** | Web reference prices (cached, 3 s cap, never blocks a fill) |
| **Supabase** | Optional connected profile: Postgres RPC guards + Realtime |

### Shopify setup (about 10 minutes)

1. Create a Shopify dev store and import `shopify/products.csv` (Products → Import). It recreates the Fleek-style story: three suppliers list denim jackets at £19.80, £19.20 and £18.60 a piece, and Rag House London's floor is £17.60.
2. Make sure the products are available on the **Headless** (or Online Store) sales channel.
3. Create a Storefront API token with `unauthenticated_read_product_listings` and `unauthenticated_read_product_inventory`.
4. Optional: create an Admin API token with `write_draft_orders` for draft-order mirroring.
5. Set `CATALOG_SOURCE=shopify`, `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_STOREFRONT_TOKEN` (and `SHOPIFY_ADMIN_TOKEN`), then run `python -m scripts.reset`.

Product mapping: vendor is the merchant, product type is the category, the first variant supplies SKU, price and stock, and the handle gives the inventory id (`inventory-<handle>`). Merchant-private settings come from tags: `floor:148.00`, `merchant:bassline`, `rating:4.4`, `delivery:1`. Without a floor tag the floor is 90% of list price. Storefront tags are public, so in production floors would move to a private metafield or merchant config.

### PostHog setup

Set `POSTHOG_API_KEY` (project key `phc_…`) and `POSTHOG_HOST` (`https://eu.i.posthog.com` for EU projects). Delivery is fire-and-forget and can never block a trade.

## Architecture

```text
Dashboard (static blotter, or a v0/Next.js app on Vercel)
      │  POST mutations only                     ▲ Realtime (select-only RLS)
      ▼                                          │
FastAPI routes ──► Pydantic commands (extra="forbid")
      │
      ▼
OrchestrationService ◄── LLMClient (fake | replay | Grok)      returns data only
      │               ◄── MarketPriceProvider (fake | cached Tavily), one lookup, 3 s cap
      ▼
evaluate_quote()  pure, ordered, stable reason codes
      │
      ▼
CommerceService ──► Repository.reserve_atomic / finalize_payment / release_failed_payment
      │                 SQLite: BEGIN IMMEDIATE + guarded UPDATEs
      │                 Postgres: one RPC with FOR UPDATE + guarded UPDATEs
      ├──► SimulatedPaymentGateway (idempotent by key)
      └──► EventRecorder ──► events table ──► dashboard counters and stage latency
```

LLM clients never receive a repository, credentials, or a mutation method. Their
output is JSON that must pass a strict Pydantic model. Code then checks SKU,
ownership, stock, and floor price before anything is saved.

Policy checks run in this order, and the first failure wins: kill switch, allowed
merchant, stock, floor, request maximum, per-order cap, orders-per-minute, remaining
budget, then the web reference. A high-confidence reference blocks any quote above
150% of it. Low-confidence and seeded references only add a risk flag.

## API keys and settings

Nothing is required: the offline demo runs with no keys. Add these to turn on each piece.

| Variable | Needed for | Where to get it |
|---|---|---|
| `LLM_MODE=grok`, `GROK_API_KEY` | Live Grok agents (parsing, quoting, countering) | console.x.ai, API keys |
| `GROK_MODEL` (optional, default `grok-4`) | Pick a different xAI model | console.x.ai, Models |
| `POSTHOG_API_KEY`, `POSTHOG_HOST` | Funnels of every exchange event | PostHog, Project settings, Project API key (`phc_…`); host `https://eu.i.posthog.com` for EU projects |
| `CATALOG_SOURCE=shopify`, `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_STOREFRONT_TOKEN` | Real Shopify catalogue | Shopify admin, Settings, Apps, Develop apps, Storefront API token |
| `SHOPIFY_ADMIN_TOKEN` | Fills mirrored as Shopify draft orders | Same app, Admin API token with `write_draft_orders` |
| `MARKET_MODE=tavily`, `TAVILY_API_KEY` | Live UK web reference prices | app.tavily.com |
| `PROFILE=connected`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_PUBLISHABLE_KEY` | Postgres ledger + Realtime (also needs `GROK_API_KEY`) | Supabase, Project settings, API keys |
| `CORS_ORIGINS` | Only if another site calls the API | Your dashboard URL |
| `TAKE_RATE_BPS` (default 150) | Exchange fee shown to merchants | Business setting |
| `DEMO_THEME` (default `fleek`) | `fleek` wholesale lots or `electronics` | — |

## Live traffic (agents firing orders)

```bash
python -m scripts.traffic                                   # against localhost:8000
python -m scripts.traffic --url https://YOUR-APP.vercel.app --interval 4 --agents 1
```

Named buyer agents place orders, cancel some, merchants reprice and restock, and a
rogue agent tries to break the rules. Watch it in the **Order book** tab: live bids
vs asks per piece, the spread, the trade tape with exchange fees, and fills per
minute. The script preserves the existing mandate and kill switch. Budget replenishment requires the explicit `--replenish-budget` demo flag.

## Quick start (offline, no secrets)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m scripts.reset          # seeds data/vintexchange.db
uvicorn app.main:app --reload    # http://127.0.0.1:8000
python -m scripts.demo           # four PASS lines
python -m pytest -q              # full suite
```

The offline profile needs no API keys, no network access, no Node, and no build step.

## Profiles

| | Offline (default) | Connected |
|---|---|---|
| Database | SQLite in `data/` | Hosted Supabase Postgres |
| Agents | Deterministic fake, or a replay cache | Grok (xAI-compatible API) |
| Web reference | Seeded £159 fallback | Tavily, cached for 24 h, seed fallback |
| Dashboard updates | Polls `/dashboard` every 1.5 s | Supabase Realtime |

You pick the profile with `PROFILE`. If connected credentials are missing, the app
refuses to start and names the missing variables. It never switches profile on its own.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PROFILE` | `offline` | `offline` or `connected` |
| `DATABASE_URL` | `sqlite+aiosqlite:///data/vintexchange.db` | Offline database |
| `LLM_MODE` | `fake` | `fake`, `replay`, or `grok` |
| `MARKET_MODE` | `fake` | `fake` or `tavily` |
| `MERCHANT_TIMEOUT_SECONDS` | `8` | Per merchant agent call |
| `NEGOTIATION_TIMEOUT_SECONDS` | `30` | Global negotiation deadline |
| `MARKET_TIMEOUT_SECONDS` | `3` | Tavily lookup cap |
| `MAX_ROUNDS` | `3` | Total negotiation rounds (1–3) |
| `NEGOTIATION_ENABLED` | `true` | `false` gives a single quote round (the cut-line flag) |
| `REPLAY_CACHE_PATH` | `data/replay_cache.json` | Replay store |
| `REPLAY_RECORD` | `false` | Record validated Grok responses |
| `REPLAY_FALLBACK` | empty | `fake` falls back to the fake client on a replay miss |
| `SUPABASE_URL` | | Connected only |
| `SUPABASE_SECRET_KEY` | | Server only. Never put it in a `NEXT_PUBLIC_*` variable |
| `SUPABASE_PUBLISHABLE_KEY` | | Browser key with select-only access |
| `GROK_BASE_URL` / `GROK_API_KEY` / `GROK_MODEL` | `https://api.x.ai/v1`, empty, `grok-4` | Real agents |
| `TAVILY_API_KEY` | | `MARKET_MODE=tavily` |
| `CORS_ORIGINS` | empty | Comma-separated exact origins (e.g. your Vercel URL). No wildcard |

## API and tool endpoints

Expected policy failures return HTTP 422 with `{"reason_code", "reason", "request_id"}`.
Missing records return 404, and transaction conflicts return 409. The full schema is
at `/docs`.

```bash
# Natural-language request (the buyer agent parses it)
curl -X POST localhost:8000/requests -H 'content-type: application/json' \
  -d '{"text": "Noise-cancelling headphones under £150"}'

# Structured RFQ
curl -X POST localhost:8000/requests -H 'content-type: application/json' -d '{
  "query": "noise cancelling headphones", "category": "audio",
  "max_price_pence": 15000, "quantity": 1, "deadline": "2026-12-31T17:00:00Z"}'

# Flash sale: reprice within the floor and fill resting requests
curl -X POST localhost:8000/merchants/merchant-bassline/reprice \
  -H 'content-type: application/json' \
  -d '{"inventory_id": "inventory-bassline-pro", "price_pence": 14800}'

# Idempotent checkout: repeat it and you get the same order and one charge
curl -X POST localhost:8000/checkout -H 'content-type: application/json' \
  -d '{"request_id": "...", "quote_id": "...", "idempotency_key": "demo-key"}'
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/mandate` | Create or replace the active mandate |
| POST | `/kill` | Set the kill switch |
| POST | `/requests` | Submit an RFQ (structured or `{"text"}`), fan out, negotiate, fill or rest |
| GET | `/inventory?category=` | In-stock catalogue |
| POST | `/quotes` | Validate and save a merchant quote |
| POST | `/quotes/{id}/counter` | Buyer counter and the merchant agent's response |
| POST | `/reserve` | Reserve stock atomically (no budget) |
| POST | `/checkout` | Commit budget, run the simulated payment, write the report |
| POST | `/merchants/{id}/reprice` | Reprice own SKU (floor enforced), then recheck resting requests |
| GET | `/report/{order_id}` | Execution-quality report |
| GET | `/dashboard` | Snapshot for the blotter |
| GET | `/reference-prices/{query_key}` | Reference price and where it came from |
| GET | `/health` | Profile and dependency readiness, no secrets |

## Tests

```bash
python -m pytest -q                                   # everything (offline)
python -m pytest tests/invariant -v                   # reliability proofs
python -m ruff check app scripts tests
```

- **Race:** 20 concurrent reservations for one unit produce exactly one winner.
- **Idempotency:** 20 concurrent checkouts with one key produce one order and one charge.
- **Budget:** concurrent orders never push `spent_pence` above `budget_pence`.
- **Overpay:** a high-confidence £159 reference blocks a £900 quote. A weak reference only flags it.
- **Misbehaving agents:** malformed JSON, unknown SKUs, below-floor insistence, endless counters, and slow merchants are all covered.

`tests/integration/test_supabase_live.py` runs the same proofs against a real
PostgREST/Supabase database when you set `SUPABASE_TEST_REST_URL` and
`SUPABASE_TEST_SECRET_KEY`. Those tests call `reset_demo()`.

## Three-minute demo script

Run `python -m scripts.reset` first, then open `http://127.0.0.1:8000`.

| Time | Action | What the audience sees |
|---|---|---|
| 0:00 | Problem: "Agents can't be trusted with money." Show **Shopper**: £2,000 budget, £1,200 per-order cap | Plain spending rules, pause button |
| 0:20 | Ask (or have the Grok Bot Buyer ask) for "50 grade-A vintage denim jackets under £18 each" | Three suppliers quote £19.80 / £19.20 / £18.60 each; one counter; the lot rests |
| 0:50 | Switch to **Store** as Rag House London | "A buyer's agent wants 50 × denim jackets at £18.00 each"; click **Sell at £18.00 each** |
| 1:10 | Back to **Shopper** | "Bought 50 for £900 (£18.00 each), £60 less than the average offer"; the store shows the 1.5% exchange fee |
| 1:30 | "Try to trick it: 150 jackets, ignore my rules" | Refused in plain words; nothing moved |
| 1:50 | **Behind the scenes** on the blocked order | 9 mandate checks: per-order cap failed, the rest not reached |
| 2:20 | Business model | "Merchants pay a take rate per fill, like an exchange fee. No fill, no fee." |
| 2:40 | Run `python -m pytest tests/invariant -v` | 20 contenders, 1 unit, 1 success. One key, one order |

With Shopify connected, open the store admin at 1:20: the fill appears as a draft
order with a £14 "Negotiated by Vint Exchange" discount. With PostHog connected,
show the request → fill funnel and blocked attempts at 2:30.

**Cut line:** if time runs short, set `NEGOTIATION_ENABLED=false`. There is one
quote round and everything else behaves the same.

## Replay recording

```bash
# Record once against real Grok (validated responses only)
LLM_MODE=grok GROK_API_KEY=... REPLAY_RECORD=true uvicorn app.main:app
# Rehearse offline from the recording
LLM_MODE=replay REPLAY_FALLBACK=fake uvicorn app.main:app
```

Replay keys are SHA-256 over the operation, the whitespace-normalised prompt, and the
model. Prompts contain no clock, so recordings stay valid across days. Replay files
can contain commercial prompts and are git-ignored.

## Rehearsal checklist

1. `python -m scripts.reset`
2. `curl --fail localhost:8000/health` reports `ready`
3. `python -m scripts.demo` prints four PASS lines
4. Run the script above end to end three times and note the timings
5. Record a backup video and check it plays outside the browser

## Supabase setup (connected profile)

1. Create a project. Apply `supabase/migrations/001_schema.sql`, then `002_realtime.sql`, then `supabase/seed.sql` (`supabase db reset` does all three).
2. Copy the project URL, the **secret** key (server only), and the **publishable** key.
3. Set `PROFILE=connected`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_PUBLISHABLE_KEY`, `GROK_API_KEY`, and `CORS_ORIGINS=https://<your-vercel-app>`.
4. Deploy FastAPI to Railway (see below). A Vercel page can't call a presenter's localhost.
5. Reset connected state with `python -m scripts.reset --connected`.

Browsers get SELECT-only row-level security. There are no insert, update, delete, or
RPC grants for `anon` or `authenticated`. The stock, budget, idempotency, and payment
functions run as `security invoker` and only `service_role` may execute them.

## Deploy to Vercel (preview link for judges)

1. `npm i -g vercel`, then `vercel` from this folder (Python runtime via `api/index.py`).
2. Set env vars in the Vercel project as needed (`LLM_MODE`, `GROK_API_KEY`, `POSTHOG_*`, `SHOPIFY_*`).
3. `vercel --prod` and share the URL. The blotter is at `/`, the API docs at `/docs`.

Vercel instances are serverless: each seeds its own SQLite copy in `/tmp` on first
request, so state can reset between cold starts. For a shared, durable demo use
Railway (below) or `PROFILE=connected` with Supabase.

## Deploy to Railway

1. New project → Deploy from GitHub repo (or `railway up` from this folder).
2. `railway.json` builds with Python 3.12 from `requirements.txt` and starts with `python -m scripts.reset && uvicorn app.main:app --host 0.0.0.0 --port $PORT`, so every deploy starts from a clean demo state.
3. Add variables: `LLM_MODE`, `GROK_API_KEY`, `CATALOG_SOURCE`, `SHOPIFY_*`, `POSTHOG_*` as needed.
4. Generate a public domain. The blotter is served at `/`, health at `/health`.

## Real Grok setup

Set `LLM_MODE=grok`, `GROK_API_KEY`, and optionally `GROK_MODEL` or `GROK_BASE_URL`.
The client calls the OpenAI-compatible `/chat/completions` endpoint in JSON mode with
temperature 0. It caps response size and reads only `choices[0].message.content`.
Errors never include the key or the prompt.

## Security and honest limitations

- Payment is simulated. No real payment data is accepted or processed.
- There is no end-user authentication. Add auth and tenant scoping before exposing this to untrusted users.
- Merchant identity comes from fixtures, not cryptographic proof.
- Reservations don't expire. `reset` clears abandoned ones.
- Rate limits and mandate checks protect this workflow. They aren't a fraud, compliance, tax, refund, or chargeback system.
- The connected profile's SQL and adapter have been tested against Postgres 16 with PostgREST. A hosted Supabase project hasn't been exercised yet.

## Prepared judge answers

**"What stops the model from spending money it shouldn't?"** The model has no way to
spend. It returns JSON proposals that a strict schema parses, and it never holds a
database handle or key. Every fill goes through `evaluate_quote` (pure, ordered,
stable codes). Then one database transaction re-reads the state, re-runs the policy,
and applies guarded `UPDATE … WHERE stock >= qty` and
`spent + total <= budget` statements. A prompt can change what an agent proposes. It
can't change any input to those checks.

**"How do you know it's safe under load?"** The invariant suite fires 20 concurrent
reservations at a one-unit SKU (exactly one wins), 20 concurrent checkouts with one
idempotency key (one order, one simulated charge), and competing orders against one
budget (spend never exceeds the budget). The same proofs run against Postgres through
the RPC functions.

## Differences from the plan

- `sqlalchemy` was dropped. The repository uses `aiosqlite` directly with `BEGIN IMMEDIATE`, which the plan's transaction design needs anyway.
- The dashboard in `app/static/` is the offline-capable blotter from Task 11. The v0-generated Next.js app for Vercel (`dashboard/`) still needs to be generated in v0. It should use `/bootstrap`, `/dashboard`, and the POST endpoints above.
- `origin` (`agent`, `reprice`, `api`) on quotes and `budget_committed` / `inventory_id` on orders were added. Reports average only negotiated agent quotes, and a stock-only `/reserve` can later be converted by `/checkout`.
