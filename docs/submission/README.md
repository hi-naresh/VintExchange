# Vint Exchange — Agentic Commerce

Vint Exchange is a unified API where buying and selling agents negotiate wholesale vintage lots, while deterministic transaction rules enforce human budgets, seller floors, and available stock.

## Judge demo (under two minutes)

Open the Agent arena and press **Run the agent arena**. Each run starts a fresh isolated market with a £2,000 shared organisation mandate. Buyer strategies submit requests, merchants quote and reprice, waiting orders fill, and a rogue request is blocked. The execution trail and results come from real application transactions, with simulated payments.

The default deterministic run produces 95 units, £1,976 simulated trade volume, £106 savings against quotes, £29.64 projected fees at 1.5%, and £24 remaining. Fees are modelled, not collected. Live Grok proposals are available when configured; the interface labels the mode. The isolated arena always uses seeded stock and reference prices and creates no external Shopify orders.

## Why it matters

Buyers need authority they can delegate without handing an agent unlimited spending power. Merchants need visible demand they can fulfil without discounting every product. The exchange connects those needs through a single request, quote, execution and reporting contract.

## Commerce depth

The code includes Shopify catalogue import and optional draft-order mirroring, PostHog event delivery, Grok negotiation adapters, Tavily reference prices, and Supabase persistence. These require configuration and are not claimed as live in the default demo. Core stock, budget and idempotency guards are tested locally.

## Business hypothesis

Merchants pay a proposed 1.5% fee on completed fills. Buyers gain governed purchasing; suppliers can answer demand with targeted prices. Expansion customers include procurement software, replenishment systems, resale marketplaces, subscription operators, and inventory-management providers. These are proposed use cases, not validated customers.

## Current limits

One organisation shares a mandate. Independent business identities, authorisation, tenant separation, real payments, fulfilment, returns and taxes are future work. The arena demonstrates price competition and budget allocation; separate invariant tests prove concurrent one-unit stock allocation. Public previews must use seeded simulated data only.

Source: https://github.com/hi-naresh/VintExchange
Event: https://gb-ecommerce-hackathon-09-2026.teamdeel.workers.dev/hackathon
