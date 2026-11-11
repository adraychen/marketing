begin;

create table public.marketing_shopify_orders (
 organization_id uuid not null references public.marketing_organizations(id),
 shop text not null, order_id text not null, created_at timestamptz not null, updated_at timestamptz not null,
 eligible boolean not null, financial_status text not null, amount numeric not null check(amount>=0),
 currency text not null, presentment jsonb not null, channel text not null, attributed boolean not null,
 utm jsonb not null, primary key (organization_id,shop,order_id)
);
create index marketing_orders_created on public.marketing_shopify_orders(organization_id,shop,created_at);
create table public.marketing_ga4_store_daily (
 organization_id uuid not null references public.marketing_organizations(id),
 property_id text not null, day date not null, source text not null, medium text not null, campaign text not null,
 metrics jsonb not null, primary key(organization_id,property_id,day,source,medium,campaign)
);
create table public.marketing_commerce_coverage (
 organization_id uuid not null references public.marketing_organizations(id),
 source text not null check(source in ('shopify_orders','ga4_store')),
 account text not null, start_day date not null, end_day date not null,
 timezone text not null, currency text, collected_at timestamptz not null default now(),
 primary key(organization_id,source,account), check(start_day<=end_day)
);
alter table public.marketing_shopify_orders enable row level security;
alter table public.marketing_ga4_store_daily enable row level security;
alter table public.marketing_commerce_coverage enable row level security;
revoke all on public.marketing_shopify_orders,public.marketing_ga4_store_daily,public.marketing_commerce_coverage
 from public,anon,authenticated;
grant select on public.marketing_shopify_orders,public.marketing_ga4_store_daily,public.marketing_commerce_coverage to authenticated;
create policy tenant_read on public.marketing_shopify_orders for select to authenticated using(marketing_private.is_member(organization_id));
create policy tenant_read on public.marketing_ga4_store_daily for select to authenticated using(marketing_private.is_member(organization_id));
create policy tenant_read on public.marketing_commerce_coverage for select to authenticated using(marketing_private.is_member(organization_id));
commit;
