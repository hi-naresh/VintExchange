-- Realtime feeds for the dashboard. Browsers subscribe with the publishable key and
-- receive only rows their select-only RLS policies allow.
do $$
declare
    t text;
begin
    if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
        create publication supabase_realtime;
    end if;
    foreach t in array array['events', 'quotes', 'orders', 'requests', 'mandates',
                             'inventory', 'exec_reports'] loop
        if not exists (
            select 1 from pg_publication_tables
             where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t
        ) then
            execute format('alter publication supabase_realtime add table public.%I', t);
        end if;
    end loop;
end $$;
