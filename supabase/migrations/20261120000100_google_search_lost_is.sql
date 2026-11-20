-- Apply before deploying the updated Google Ads collector and report reader.
alter table public.marketing_performance_daily
    add column if not exists search_budget_lost_impression_share double precision,
    add column if not exists search_rank_lost_impression_share double precision;
-- Existing observations remain NULL until reimported. Existing RLS remains unchanged.
