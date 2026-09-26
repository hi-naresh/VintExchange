-- Vint Exchange SQLite schema. Money is integer pence. Timestamps are fixed-width
-- UTC ISO strings (YYYY-MM-DDTHH:MM:SS.ffffffZ) so they compare lexicographically.

CREATE TABLE IF NOT EXISTS mandates (
    id                   TEXT PRIMARY KEY,
    budget_pence         INTEGER NOT NULL CHECK (budget_pence > 0),
    spent_pence          INTEGER NOT NULL DEFAULT 0 CHECK (spent_pence >= 0),
    max_per_order_pence  INTEGER NOT NULL CHECK (max_per_order_pence > 0),
    allowed_merchant_ids TEXT NOT NULL CHECK (json_valid(allowed_merchant_ids)),
    orders_per_minute    INTEGER NOT NULL CHECK (orders_per_minute > 0),
    killed               INTEGER NOT NULL DEFAULT 0 CHECK (killed IN (0, 1)),
    active               INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CHECK (spent_pence <= budget_pence),
    CHECK (max_per_order_pence <= budget_pence)
);

CREATE TABLE IF NOT EXISTS merchants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    rating     REAL NOT NULL CHECK (rating >= 0 AND rating <= 5),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    id                TEXT PRIMARY KEY,
    sku               TEXT NOT NULL,
    merchant_id       TEXT NOT NULL REFERENCES merchants(id),
    title             TEXT NOT NULL,
    category          TEXT NOT NULL,
    list_price_pence  INTEGER NOT NULL CHECK (list_price_pence > 0),
    floor_price_pence INTEGER NOT NULL CHECK (floor_price_pence > 0),
    stock             INTEGER NOT NULL CHECK (stock >= 0),
    delivery_days     INTEGER NOT NULL CHECK (delivery_days >= 0),
    -- Source-platform identity (e.g. Shopify variant GID) and its original price.
    external_id          TEXT,
    external_price_pence INTEGER CHECK (external_price_pence IS NULL OR external_price_pence > 0),
    UNIQUE (merchant_id, sku),
    CHECK (list_price_pence >= floor_price_pence)
);
CREATE INDEX IF NOT EXISTS idx_inventory_category ON inventory (category);
CREATE INDEX IF NOT EXISTS idx_inventory_merchant ON inventory (merchant_id);

CREATE TABLE IF NOT EXISTS requests (
    id              TEXT PRIMARY KEY,
    mandate_id      TEXT NOT NULL REFERENCES mandates(id),
    query           TEXT NOT NULL,
    category        TEXT NOT NULL,
    max_price_pence INTEGER NOT NULL CHECK (max_price_pence > 0),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    deadline        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('requested', 'quoted', 'negotiating',
                        'resting', 'reserved', 'paid', 'rejected', 'out_of_stock',
                        'cancelled')),
    round           INTEGER NOT NULL DEFAULT 0 CHECK (round >= 0 AND round <= 3),
    agent           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_status_category ON requests (status, category, created_at);
CREATE INDEX IF NOT EXISTS idx_requests_mandate ON requests (mandate_id);

CREATE TABLE IF NOT EXISTS quotes (
    id               TEXT PRIMARY KEY,
    request_id       TEXT NOT NULL REFERENCES requests(id),
    merchant_id      TEXT NOT NULL REFERENCES merchants(id),
    inventory_id     TEXT REFERENCES inventory(id),
    price_pence      INTEGER CHECK (price_pence IS NULL OR price_pence > 0),
    delivery_days    INTEGER CHECK (delivery_days IS NULL OR delivery_days >= 0),
    round            INTEGER NOT NULL CHECK (round >= 0 AND round <= 3),
    status           TEXT NOT NULL CHECK (status IN ('proposed', 'valid', 'countered',
                         'accepted', 'rejected')),
    origin           TEXT NOT NULL DEFAULT 'agent' CHECK (origin IN ('agent', 'reprice', 'api')),
    rejection_reason TEXT,
    created_at       TEXT NOT NULL,
    CHECK (status = 'rejected' OR (inventory_id IS NOT NULL AND price_pence IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_quotes_request ON quotes (request_id);
CREATE INDEX IF NOT EXISTS idx_quotes_merchant ON quotes (merchant_id);
CREATE INDEX IF NOT EXISTS idx_quotes_inventory ON quotes (inventory_id);

CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    request_id        TEXT NOT NULL UNIQUE REFERENCES requests(id),
    quote_id          TEXT NOT NULL REFERENCES quotes(id),
    mandate_id        TEXT NOT NULL REFERENCES mandates(id),
    inventory_id      TEXT NOT NULL REFERENCES inventory(id),
    idempotency_key   TEXT NOT NULL UNIQUE,
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    price_pence       INTEGER NOT NULL CHECK (price_pence > 0),
    budget_committed  INTEGER NOT NULL DEFAULT 0 CHECK (budget_committed IN (0, 1)),
    payment_reference TEXT,
    status            TEXT NOT NULL CHECK (status IN ('reserved', 'paid', 'payment_failed')),
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_mandate_created ON orders (mandate_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_quote ON orders (quote_id);

CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    type       TEXT NOT NULL,
    request_id TEXT,
    payload    TEXT NOT NULL CHECK (json_valid(payload)),
    latency_ms REAL CHECK (latency_ms IS NULL OR latency_ms >= 0),
    stage      TEXT NOT NULL CHECK (stage IN ('llm', 'policy', 'database', 'system'))
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_at);
CREATE INDEX IF NOT EXISTS idx_events_request ON events (request_id);

CREATE TABLE IF NOT EXISTS exec_reports (
    id                   TEXT PRIMARY KEY,
    order_id             TEXT NOT NULL UNIQUE REFERENCES orders(id),
    paid_pence           INTEGER NOT NULL CHECK (paid_pence >= 0),
    best_quote_pence     INTEGER NOT NULL CHECK (best_quote_pence >= 0),
    average_quote_pence  INTEGER NOT NULL CHECK (average_quote_pence >= 0),
    saved_pence          INTEGER NOT NULL CHECK (saved_pence >= 0),
    web_reference_pence  INTEGER CHECK (web_reference_pence IS NULL OR web_reference_pence >= 0),
    reference_source     TEXT,
    reference_confidence TEXT,
    reference_urls       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(reference_urls)),
    summary              TEXT NOT NULL,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reference_prices (
    id           TEXT PRIMARY KEY,
    query_key    TEXT NOT NULL,
    product_name TEXT NOT NULL,
    country      TEXT NOT NULL,
    currency     TEXT NOT NULL,
    price_pence  INTEGER NOT NULL CHECK (price_pence > 0),
    source       TEXT NOT NULL CHECK (source IN ('tavily_live', 'tavily_cache', 'seed_fallback')),
    confidence   TEXT NOT NULL CHECK (confidence IN ('high', 'low')),
    source_urls  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_urls)),
    fetched_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    UNIQUE (query_key, country, currency)
);
