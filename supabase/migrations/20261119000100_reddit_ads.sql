begin;
-- Reuse platform connections, accounts, campaigns, ad groups, ads and sync history.
-- Existing Google daily rows require non-null conversions and cannot represent
-- event-specific attribution or independent Reddit breakdowns safely.
create table public.marketing_paid_observations (
 organization_id uuid not null,
 account_id uuid not null,
 platform text not null,
 day date not null,
 ad_id text not null,
 ad_group_id text not null,
 campaign_id text not null,
 breakdown text not null default 'base',
 segment text not null default '',
 currency text not null,
 data jsonb not null,
 synced_at timestamptz not null default now(),
 primary key (organization_id,account_id,day,ad_id,breakdown,segment),
 foreign key (organization_id,account_id) references public.marketing_ad_accounts(organization_id,id) on delete cascade
);
alter table public.marketing_paid_observations enable row level security;
revoke all on public.marketing_paid_observations from public,anon,authenticated;
grant select on public.marketing_paid_observations to authenticated;
create policy tenant_read on public.marketing_paid_observations for select to authenticated
 using (marketing_private.is_member(organization_id));

-- Never expose OAuth secrets through public API reads, even to organization members.
create table marketing_private.reddit_tokens (
 organization_id uuid primary key references public.marketing_organizations(id) on delete cascade,
 ciphertext text not null,
 updated_at timestamptz not null default now()
);
revoke all on marketing_private.reddit_tokens from public,anon,authenticated;
commit;
