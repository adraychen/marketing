begin;
create table public.marketing_meta_platform_daily (
 organization_id uuid not null, account_id text not null, ad_id text not null,
 day date not null, publisher_platform text not null, data jsonb not null,
 primary key(organization_id,account_id,ad_id,day,publisher_platform),
 foreign key(organization_id,account_id) references public.marketing_meta_ads_accounts(organization_id,account_id)
);
alter table public.marketing_meta_platform_daily enable row level security;
revoke all on public.marketing_meta_platform_daily from public,anon,authenticated;
grant select on public.marketing_meta_platform_daily to authenticated;
create policy tenant_read on public.marketing_meta_platform_daily for select to authenticated
 using(marketing_private.is_member(organization_id));
commit;
