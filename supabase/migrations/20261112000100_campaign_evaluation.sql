begin;
alter table public.marketing_shopify_orders add column evaluation jsonb not null default '{}';
create table public.marketing_evaluation_snapshots (
 organization_id uuid not null references public.marketing_organizations(id),
 source text not null check(source in ('shopify_products','ga4_landing')),
 account text not null, start_day date, end_day date,
 data jsonb not null, collected_at timestamptz not null default now(),
 primary key(organization_id,source,account)
);
create table public.marketing_campaign_cost_models (
 organization_id uuid not null references public.marketing_organizations(id),
 account_id text not null, campaign_id text not null, data jsonb not null,
 updated_at timestamptz not null default now(),
 primary key(organization_id,account_id,campaign_id)
);
alter table public.marketing_evaluation_snapshots enable row level security;
alter table public.marketing_campaign_cost_models enable row level security;
revoke all on public.marketing_evaluation_snapshots,public.marketing_campaign_cost_models from public,anon,authenticated;
grant select on public.marketing_evaluation_snapshots,public.marketing_campaign_cost_models to authenticated;
create policy tenant_read on public.marketing_evaluation_snapshots for select to authenticated using(marketing_private.is_member(organization_id));
create policy tenant_read on public.marketing_campaign_cost_models for select to authenticated using(marketing_private.is_member(organization_id));
commit;
