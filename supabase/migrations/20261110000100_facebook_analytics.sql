begin;

create table public.marketing_facebook_page_metrics (
    organization_id uuid not null references public.marketing_organizations(id),
    page_id text not null,
    metric text not null,
    api_version text not null,
    day date not null,
    basis text not null check (basis in ('day', 'point', 'status')),
    value numeric,
    period_end_at timestamptz,
    reporting_offset text,
    availability text not null,
    collected_at timestamptz not null default now(),
    primary key (organization_id, page_id, metric, api_version, day, basis)
);

create table public.marketing_facebook_manual (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.marketing_organizations(id),
    page_id text not null,
    item_id uuid,
    metric text not null,
    value numeric not null,
    unit text not null,
    basis text not null check (basis in ('period', 'point', 'lifetime')),
    period_start date not null,
    period_end date not null,
    observed_day date not null,
    reporting_offset text not null default '',
    notes text not null default '',
    updated_at timestamptz not null default now(),
    check (period_start <= period_end and period_end <= observed_day),
    check (basis = 'period' or period_start = period_end and period_end = observed_day),
    foreign key (organization_id, item_id) references public.marketing_content_items(organization_id, id)
);

create unique index marketing_facebook_manual_identity on public.marketing_facebook_manual
    (organization_id, page_id, (coalesce(item_id::text, '')), metric, basis, period_start, period_end, reporting_offset);

alter table public.marketing_facebook_page_metrics enable row level security;
alter table public.marketing_facebook_manual enable row level security;
revoke all on public.marketing_facebook_page_metrics, public.marketing_facebook_manual from public, anon, authenticated;
grant select on public.marketing_facebook_page_metrics, public.marketing_facebook_manual to authenticated;
create policy tenant_read on public.marketing_facebook_page_metrics for select to authenticated
    using (marketing_private.is_member(organization_id));
create policy tenant_read on public.marketing_facebook_manual for select to authenticated
    using (marketing_private.is_member(organization_id));

commit;
