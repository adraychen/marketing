begin;
create table public.marketing_meta_ads_accounts (
 organization_id uuid not null references public.marketing_organizations(id),
 account_id text not null, data jsonb not null,
 synced_from date not null, synced_through date not null, synced_at timestamptz not null default now(),
 primary key(organization_id,account_id), check(synced_from<=synced_through)
);
create table public.marketing_meta_ads_resources (
 organization_id uuid not null, account_id text not null, kind text not null check(kind in ('campaigns','adsets','ads')),
 resource_id text not null, data jsonb not null, last_seen_at timestamptz not null default now(),
 primary key(organization_id,account_id,kind,resource_id),
 foreign key(organization_id,account_id) references public.marketing_meta_ads_accounts(organization_id,account_id)
);
create table public.marketing_meta_ads_daily (
 organization_id uuid not null, account_id text not null, ad_id text not null, day date not null,
 campaign_id text not null, adset_id text not null, data jsonb not null,
 primary key(organization_id,account_id,ad_id,day),
 foreign key(organization_id,account_id) references public.marketing_meta_ads_accounts(organization_id,account_id)
);
create index marketing_meta_ads_campaign_dates on public.marketing_meta_ads_daily(organization_id,account_id,campaign_id,day);
create table public.marketing_meta_ads_sync_runs (
 id uuid primary key default gen_random_uuid(),
 organization_id uuid not null references public.marketing_organizations(id), account_id text not null,
 status text not null check(status in ('succeeded','failed')), counts jsonb not null default '{}',
 message text, finished_at timestamptz not null default now()
);
create index marketing_meta_ads_sync_recent on public.marketing_meta_ads_sync_runs(organization_id,finished_at desc);
do $$
declare t text;
begin
 foreach t in array array['marketing_meta_ads_accounts','marketing_meta_ads_resources','marketing_meta_ads_daily','marketing_meta_ads_sync_runs'] loop
 execute format('alter table public.%I enable row level security',t);
 execute format('revoke all on public.%I from public,anon,authenticated',t);
 execute format('grant select on public.%I to authenticated',t);
 execute format('create policy tenant_read on public.%I for select to authenticated using(marketing_private.is_member(organization_id))',t);
 end loop;
end $$;
commit;
