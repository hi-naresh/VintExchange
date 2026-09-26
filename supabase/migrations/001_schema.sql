-- Vint Exchange — Postgres schema, constraints, and transaction RPCs.
-- Money is integer pence (bigint). Text IDs match the deterministic seed.
-- Browser clients get select-only RLS; every mutation goes through FastAPI, which
-- calls these functions with the service role.

create table if not exists public.mandates (
    id                   text primary key,
    budget_pence         bigint not null check (budget_pence > 0),
    spent_pence          bigint not null default 0 check (spent_pence >= 0),
    max_per_order_pence  bigint not null check (max_per_order_pence > 0),
    allowed_merchant_ids text[] not null check (cardinality(allowed_merchant_ids) > 0),
    orders_per_minute    integer not null check (orders_per_minute > 0),
    killed               boolean not null default false,
    active               boolean not null default true,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    check (spent_pence <= budget_pence),
    check (max_per_order_pence <= budget_pence)
);
create unique index if not exists mandates_one_active on public.mandates (active) where active;

create table if not exists public.merchants (
    id         text primary key,
    name       text not null,
    rating     numeric(2, 1) not null check (rating >= 0 and rating <= 5),
    created_at timestamptz not null default now()
);

create table if not exists public.inventory (
    id                text primary key,
    sku               text not null,
    merchant_id       text not null references public.merchants (id),
    title             text not null,
    category          text not null,
    list_price_pence  bigint not null check (list_price_pence > 0),
    floor_price_pence bigint not null check (floor_price_pence > 0),
    stock             integer not null check (stock >= 0),
    delivery_days     integer not null check (delivery_days >= 0),
    external_id          text,
    external_price_pence bigint check (external_price_pence is null or external_price_pence > 0),
    unique (merchant_id, sku),
    check (list_price_pence >= floor_price_pence)
);
create index if not exists inventory_category_idx on public.inventory (category);
create index if not exists inventory_merchant_idx on public.inventory (merchant_id);

create table if not exists public.requests (
    id              text primary key,
    mandate_id      text not null references public.mandates (id),
    query           text not null,
    category        text not null,
    max_price_pence bigint not null check (max_price_pence > 0),
    quantity        integer not null check (quantity > 0),
    deadline        timestamptz not null,
    status          text not null check (status in ('requested', 'quoted', 'negotiating',
                        'resting', 'reserved', 'paid', 'rejected', 'out_of_stock',
                        'cancelled')),
    round           integer not null default 0 check (round between 0 and 3),
    agent           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index if not exists requests_status_category_idx
    on public.requests (status, category, created_at);
create index if not exists requests_mandate_idx on public.requests (mandate_id);

create table if not exists public.quotes (
    id               text primary key,
    request_id       text not null references public.requests (id),
    merchant_id      text not null references public.merchants (id),
    inventory_id     text references public.inventory (id),
    price_pence      bigint check (price_pence is null or price_pence > 0),
    delivery_days    integer check (delivery_days is null or delivery_days >= 0),
    round            integer not null check (round between 0 and 3),
    status           text not null check (status in ('proposed', 'valid', 'countered',
                         'accepted', 'rejected')),
    origin           text not null default 'agent' check (origin in ('agent', 'reprice', 'api')),
    rejection_reason text,
    created_at       timestamptz not null default now(),
    check (status = 'rejected' or (inventory_id is not null and price_pence is not null))
);
create index if not exists quotes_request_idx on public.quotes (request_id);
create index if not exists quotes_merchant_idx on public.quotes (merchant_id);
create index if not exists quotes_inventory_idx on public.quotes (inventory_id);

create table if not exists public.orders (
    id                text primary key,
    request_id        text not null references public.requests (id),
    quote_id          text not null references public.quotes (id),
    mandate_id        text not null references public.mandates (id),
    inventory_id      text not null references public.inventory (id),
    idempotency_key   text not null,
    quantity          integer not null check (quantity > 0),
    price_pence       bigint not null check (price_pence > 0),
    budget_committed  boolean not null default false,
    payment_reference text,
    status            text not null check (status in ('reserved', 'paid', 'payment_failed')),
    created_at        timestamptz not null default now(),
    constraint orders_idempotency_key_unique unique (idempotency_key),
    constraint orders_one_per_request unique (request_id)
);
create index if not exists orders_mandate_created_idx on public.orders (mandate_id, created_at);
create index if not exists orders_quote_idx on public.orders (quote_id);
create index if not exists orders_inventory_idx on public.orders (inventory_id);

create table if not exists public.events (
    id         text primary key,
    created_at timestamptz not null default now(),
    type       text not null,
    request_id text,
    payload    jsonb not null default '{}'::jsonb,
    latency_ms double precision check (latency_ms is null or latency_ms >= 0),
    stage      text not null check (stage in ('llm', 'policy', 'database', 'system'))
);
create index if not exists events_created_idx on public.events (created_at desc);
create index if not exists events_request_idx on public.events (request_id);

create table if not exists public.exec_reports (
    id                   text primary key,
    order_id             text not null unique references public.orders (id),
    paid_pence           bigint not null check (paid_pence >= 0),
    best_quote_pence     bigint not null check (best_quote_pence >= 0),
    average_quote_pence  bigint not null check (average_quote_pence >= 0),
    saved_pence          bigint not null check (saved_pence >= 0),
    web_reference_pence  bigint check (web_reference_pence is null or web_reference_pence >= 0),
    reference_source     text,
    reference_confidence text,
    reference_urls       jsonb not null default '[]'::jsonb,
    summary              text not null,
    created_at           timestamptz not null default now()
);

create table if not exists public.reference_prices (
    id           text primary key,
    query_key    text not null,
    product_name text not null,
    country      text not null,
    currency     text not null,
    price_pence  bigint not null check (price_pence > 0),
    source       text not null check (source in ('tavily_live', 'tavily_cache', 'seed_fallback')),
    confidence   text not null check (confidence in ('high', 'low')),
    source_urls  jsonb not null default '[]'::jsonb,
    fetched_at   timestamptz not null,
    expires_at   timestamptz not null,
    unique (query_key, country, currency)
);

-- ------------------------------------------------------------------ row-level security
-- Browsers (anon/authenticated) may only SELECT. No insert/update/delete policies exist.

do $$
declare
    t text;
begin
    foreach t in array array['mandates', 'merchants', 'inventory', 'requests', 'quotes',
                             'orders', 'events', 'exec_reports', 'reference_prices'] loop
        execute format('alter table public.%I enable row level security', t);
        execute format('drop policy if exists %I on public.%I', t || '_read', t);
        execute format(
            'create policy %I on public.%I for select to anon, authenticated using (true)',
            t || '_read', t);
        execute format('revoke insert, update, delete, truncate on public.%I from anon, authenticated', t);
        execute format('grant select on public.%I to anon, authenticated', t);
    end loop;
end $$;

-- ------------------------------------------------------------ transaction functions
-- security invoker: they run with the caller's rights (the service role via FastAPI).

create or replace function public.set_active_mandate(
    p_id text, p_budget_pence bigint, p_max_per_order_pence bigint,
    p_allowed_merchant_ids text[], p_orders_per_minute integer
) returns public.mandates
language plpgsql security invoker set search_path = public as $$
declare
    v_row public.mandates;
begin
    update mandates set active = false, updated_at = now() where active;
    insert into mandates (id, budget_pence, spent_pence, max_per_order_pence,
                          allowed_merchant_ids, orders_per_minute, killed, active)
    values (p_id, p_budget_pence, 0, p_max_per_order_pence, p_allowed_merchant_ids,
            p_orders_per_minute, false, true)
    returning * into v_row;
    return v_row;
end $$;

-- Atomically reserve stock (and optionally commit budget) for one quote.
-- Returns {"accepted": bool, "code": text, "replayed": bool, "order": {...} | null}.
create or replace function public.reserve_atomic(
    p_order_id text, p_request_id text, p_quote_id text, p_idempotency_key text,
    p_commit_budget boolean, p_quantity integer, p_total bigint
) returns jsonb
language plpgsql security invoker set search_path = public as $$
declare
    v_existing public.orders;
    v_request  public.requests;
    v_quote    public.quotes;
    v_mandate  public.mandates;
    v_order    public.orders;
    v_rows     integer;
begin
    -- Lock the request first so concurrent calls for it serialize; then check the key,
    -- so a waiter sees the winner's order instead of a closed request.
    select * into v_request from requests where id = p_request_id for update;
    if not found then
        raise exception 'request % not found', p_request_id using errcode = 'P0002';
    end if;

    select * into v_existing from orders where idempotency_key = p_idempotency_key for update;
    if found then
        if v_existing.request_id <> p_request_id or v_existing.quote_id <> p_quote_id then
            return jsonb_build_object('accepted', false, 'code', 'IDEMPOTENCY_KEY_REUSED',
                                      'replayed', true, 'order', null);
        end if;
        if v_existing.status = 'payment_failed' then
            return jsonb_build_object('accepted', false, 'code', 'PAYMENT_FAILED',
                                      'replayed', true, 'order', to_jsonb(v_existing));
        end if;
        if p_commit_budget and not v_existing.budget_committed then
            select * into v_mandate from mandates where id = v_existing.mandate_id for update;
            if v_mandate.killed then
                return jsonb_build_object('accepted', false, 'code', 'KILL_SWITCH_ON',
                                          'replayed', true, 'order', to_jsonb(v_existing));
            end if;
            update mandates set spent_pence = spent_pence + p_total, updated_at = now()
             where id = v_mandate.id and spent_pence + p_total <= budget_pence;
            get diagnostics v_rows = row_count;
            if v_rows <> 1 then
                return jsonb_build_object('accepted', false, 'code', 'BUDGET_EXCEEDED',
                                          'replayed', true, 'order', to_jsonb(v_existing));
            end if;
            update orders set budget_committed = true
             where id = v_existing.id and not budget_committed
            returning * into v_existing;
            return jsonb_build_object('accepted', true, 'code', 'ALLOWED', 'replayed', false,
                                      'order', to_jsonb(v_existing));
        end if;
        return jsonb_build_object('accepted', true, 'code', 'ALLOWED', 'replayed', true,
                                  'order', to_jsonb(v_existing));
    end if;

    if v_request.status not in ('requested', 'quoted', 'negotiating', 'resting') then
        return jsonb_build_object('accepted', false, 'code', 'REQUEST_CLOSED',
                                  'replayed', false, 'order', null);
    end if;
    select * into v_quote from quotes where id = p_quote_id and request_id = p_request_id;
    if not found or v_quote.status = 'rejected' then
        raise exception 'quote % not usable', p_quote_id using errcode = 'P0002';
    end if;
    select * into v_mandate from mandates where active for update;
    if not found then
        return jsonb_build_object('accepted', false, 'code', 'NO_ACTIVE_MANDATE',
                                  'replayed', false, 'order', null);
    end if;
    if v_mandate.killed then
        return jsonb_build_object('accepted', false, 'code', 'KILL_SWITCH_ON',
                                  'replayed', false, 'order', null);
    end if;

    -- Stock guard.
    update inventory set stock = stock - p_quantity
     where id = v_quote.inventory_id and stock >= p_quantity;
    get diagnostics v_rows = row_count;
    if v_rows <> 1 then
        return jsonb_build_object('accepted', false, 'code', 'OUT_OF_STOCK',
                                  'replayed', false, 'order', null);
    end if;

    -- Budget guard.
    if p_commit_budget then
        update mandates set spent_pence = spent_pence + p_total, updated_at = now()
         where id = v_mandate.id and spent_pence + p_total <= budget_pence;
        get diagnostics v_rows = row_count;
        if v_rows <> 1 then
            raise exception 'BUDGET_EXCEEDED' using errcode = 'P0001';
        end if;
    end if;

    insert into orders (id, request_id, quote_id, mandate_id, inventory_id, idempotency_key,
                        quantity, price_pence, budget_committed, status)
    values (p_order_id, p_request_id, p_quote_id, v_mandate.id, v_quote.inventory_id,
            p_idempotency_key, p_quantity, v_quote.price_pence, p_commit_budget, 'reserved')
    returning * into v_order;
    update requests set status = 'reserved', updated_at = now() where id = p_request_id;
    update quotes set status = 'accepted' where id = p_quote_id;
    return jsonb_build_object('accepted', true, 'code', 'ALLOWED', 'replayed', false,
                              'order', to_jsonb(v_order));
exception
    when raise_exception then
        if sqlerrm = 'BUDGET_EXCEEDED' then
            -- The whole block (including the stock decrement) is rolled back.
            return jsonb_build_object('accepted', false, 'code', 'BUDGET_EXCEEDED',
                                      'replayed', false, 'order', null);
        end if;
        raise;
end $$;

-- Mark a reserved, budget-committed order paid and write its report exactly once.
create or replace function public.checkout_atomic(
    p_order_id text, p_payment_reference text, p_report jsonb
) returns jsonb
language plpgsql security invoker set search_path = public as $$
declare
    v_order  public.orders;
    v_report public.exec_reports;
begin
    select * into v_order from orders where id = p_order_id for update;
    if not found then
        raise exception 'order % not found', p_order_id using errcode = 'P0002';
    end if;
    if v_order.status <> 'reserved' then
        select * into v_report from exec_reports where order_id = p_order_id;
        return jsonb_build_object('order', to_jsonb(v_order),
                                  'report', case when found then to_jsonb(v_report) end);
    end if;
    if not v_order.budget_committed then
        raise exception 'order % has no committed budget', p_order_id using errcode = '23514';
    end if;
    update orders set status = 'paid', payment_reference = p_payment_reference
     where id = p_order_id and status = 'reserved'
    returning * into v_order;
    update requests set status = 'paid', updated_at = now() where id = v_order.request_id;
    insert into exec_reports (id, order_id, paid_pence, best_quote_pence, average_quote_pence,
                              saved_pence, web_reference_pence, reference_source,
                              reference_confidence, reference_urls, summary)
    values ('report-' || substr(md5(p_order_id), 1, 12), p_order_id,
            (p_report ->> 'paid_pence')::bigint, (p_report ->> 'best_quote_pence')::bigint,
            (p_report ->> 'average_quote_pence')::bigint, (p_report ->> 'saved_pence')::bigint,
            (p_report ->> 'web_reference_pence')::bigint, p_report ->> 'reference_source',
            p_report ->> 'reference_confidence',
            coalesce(p_report -> 'reference_urls', '[]'::jsonb), p_report ->> 'summary')
    returning * into v_report;
    return jsonb_build_object('order', to_jsonb(v_order), 'report', to_jsonb(v_report));
end $$;

-- Restore stock and budget exactly once after a failed simulated payment.
create or replace function public.release_failed_payment(p_order_id text)
returns public.orders
language plpgsql security invoker set search_path = public as $$
declare
    v_order public.orders;
begin
    update orders set status = 'payment_failed'
     where id = p_order_id and status = 'reserved'
    returning * into v_order;
    if found then
        update inventory set stock = stock + v_order.quantity where id = v_order.inventory_id;
        if v_order.budget_committed then
            update mandates set spent_pence = spent_pence - v_order.quantity * v_order.price_pence,
                                updated_at = now()
             where id = v_order.mandate_id
               and spent_pence >= v_order.quantity * v_order.price_pence;
        end if;
        update requests set status = 'rejected', updated_at = now()
         where id = v_order.request_id;
    else
        select * into v_order from orders where id = p_order_id;
    end if;
    return v_order;
end $$;

-- Wipe runtime rows and restore the deterministic seed (see supabase/seed.sql).
create or replace function public.reset_demo() returns void
language plpgsql security invoker set search_path = public as $$
begin
    truncate exec_reports, orders, quotes, events, requests, reference_prices,
             inventory, merchants, mandates;
    perform public.seed_demo();
end $$;

-- Execution is restricted to the service role (FastAPI). Browsers cannot call RPCs.
revoke execute on function public.set_active_mandate(text, bigint, bigint, text[], integer)
    from public, anon, authenticated;
revoke execute on function public.reserve_atomic(text, text, text, text, boolean, integer, bigint)
    from public, anon, authenticated;
revoke execute on function public.checkout_atomic(text, text, jsonb)
    from public, anon, authenticated;
revoke execute on function public.release_failed_payment(text)
    from public, anon, authenticated;
revoke execute on function public.reset_demo() from public, anon, authenticated;

grant execute on function public.set_active_mandate(text, bigint, bigint, text[], integer)
    to service_role;
grant execute on function public.reserve_atomic(text, text, text, text, boolean, integer, bigint)
    to service_role;
grant execute on function public.checkout_atomic(text, text, jsonb) to service_role;
grant execute on function public.release_failed_payment(text) to service_role;
grant execute on function public.reset_demo() to service_role;
