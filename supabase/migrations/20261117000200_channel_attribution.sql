begin;
alter table public.marketing_commerce_coverage drop constraint marketing_commerce_coverage_source_check;
alter table public.marketing_commerce_coverage add constraint marketing_commerce_coverage_source_check
 check(source in ('shopify_orders','ga4_store','ga4_channels'));
create table public.marketing_channel_daily (
 organization_id uuid not null references public.marketing_organizations(id),
 property_id text not null, day date not null, segment text not null,
 dimensions jsonb not null, metrics jsonb not null,
 primary key(organization_id,property_id,day,segment)
);
alter table public.marketing_channel_daily enable row level security;
revoke all on public.marketing_channel_daily from public,anon,authenticated;
grant select on public.marketing_channel_daily to authenticated;
create policy tenant_read on public.marketing_channel_daily for select to authenticated
 using(marketing_private.is_member(organization_id));
commit;
