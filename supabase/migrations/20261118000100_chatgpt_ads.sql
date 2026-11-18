begin;
create table public.marketing_chatgpt_ads_reports (
 organization_id uuid primary key references public.marketing_organizations(id), account_id text not null,
 start_day date not null, end_day date not null, data jsonb not null,
 synced_at timestamptz not null default now(), check(start_day<=end_day)
);
create table public.marketing_chatgpt_ads_sync_runs (
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references public.marketing_organizations(id),
 status text not null check(status in ('succeeded','failed')), message text, finished_at timestamptz not null default now()
);
do $$ declare t text; begin
 foreach t in array array['marketing_chatgpt_ads_reports','marketing_chatgpt_ads_sync_runs'] loop
 execute format('alter table public.%I enable row level security',t);
 execute format('revoke all on public.%I from public,anon,authenticated',t);
 execute format('grant select on public.%I to authenticated',t);
 execute format('create policy tenant_read on public.%I for select to authenticated using(marketing_private.is_member(organization_id))',t);
 end loop;
end $$;
commit;
