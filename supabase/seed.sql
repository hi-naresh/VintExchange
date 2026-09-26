-- Deterministic demo seed. Generated from app/repositories/seed.py; keep in sync
-- (tests/unit/test_supabase_contract.py checks the IDs and prices).

create or replace function public.seed_demo() returns void
language plpgsql security invoker set search_path = public as $$
begin
    insert into merchants (id, name, rating) values ('merchant-aurora', 'Aurora Audio', 4.6);
    insert into merchants (id, name, rating) values ('merchant-bassline', 'Bassline Supply', 4.4);
    insert into merchants (id, name, rating) values ('merchant-circuit', 'Circuit Electronics', 4.2);
    insert into inventory (id, sku, merchant_id, title, category, list_price_pence, floor_price_pence, stock, delivery_days)
    values ('inventory-aurora-studio', 'AUR-STUDIO', 'merchant-aurora', 'Aurora Studio ANC Headphones', 'audio', 16600, 15500, 5, 2);
    insert into inventory (id, sku, merchant_id, title, category, list_price_pence, floor_price_pence, stock, delivery_days)
    values ('inventory-bassline-pro', 'BSL-PRO', 'merchant-bassline', 'Bassline Pro Wireless Headphones', 'audio', 16200, 14800, 5, 1);
    insert into inventory (id, sku, merchant_id, title, category, list_price_pence, floor_price_pence, stock, delivery_days)
    values ('inventory-circuit-q45', 'CIR-Q45', 'merchant-circuit', 'Circuit Q45 Noise-Cancelling Headphones', 'audio', 15800, 15200, 5, 3);
    insert into inventory (id, sku, merchant_id, title, category, list_price_pence, floor_price_pence, stock, delivery_days)
    values ('inventory-aurora-reference', 'AUR-REF', 'merchant-aurora', 'Aurora Reference Pro Headphones', 'premium-audio', 90000, 85000, 2, 2);
    insert into inventory (id, sku, merchant_id, title, category, list_price_pence, floor_price_pence, stock, delivery_days)
    values ('inventory-circuit-buds-ltd', 'CIR-BUDS-LTD', 'merchant-circuit', 'Circuit Buds Limited Edition', 'earbuds', 4900, 4500, 1, 1);
    insert into inventory (id, sku, merchant_id, title, category, list_price_pence, floor_price_pence, stock, delivery_days)
    values ('inventory-bassline-deck', 'BSL-DECK', 'merchant-bassline', 'Bassline Deck Turntable', 'turntables', 24000, 22000, 0, 4);
    perform public.set_active_mandate('mandate-demo', 30000, 20000, array['merchant-aurora', 'merchant-bassline', 'merchant-circuit'], 5);
end $$;

revoke execute on function public.seed_demo() from public, anon, authenticated;
grant execute on function public.seed_demo() to service_role;

select public.reset_demo();
