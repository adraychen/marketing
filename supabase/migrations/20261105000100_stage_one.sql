begin;

create table public.marketing_organizations (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.marketing_memberships (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.marketing_organizations(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    role text not null default 'member' check (role in ('admin', 'member')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, user_id)
);
create index marketing_memberships_user on public.marketing_memberships(user_id);

create table public.marketing_platform_connections (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.marketing_organizations(id) on delete cascade,
    platform text not null,
    credential_ref text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, platform),
    unique (organization_id, id)
);

create table public.marketing_ad_accounts (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.marketing_organizations(id) on delete cascade,
    connection_id uuid not null,
    resource text not null,
    data jsonb not null,
    synced_from date,
    synced_through date,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, id),
    unique (connection_id, resource),
    foreign key (organization_id, connection_id) references public.marketing_platform_connections(organization_id, id)
);

do $$
declare
    entry record;
begin
    for entry in select * from (values
        ('marketing_campaigns', null), ('marketing_ad_groups', 'marketing_campaigns'), ('marketing_ads', 'marketing_ad_groups'),
        ('marketing_keywords', 'marketing_ad_groups'), ('marketing_search_terms', 'marketing_ad_groups')
    ) as config(table_name, parent_table)
    loop
        execute format('create table public.%I (
            id uuid primary key default gen_random_uuid(),
            organization_id uuid not null,
            account_id uuid not null,
            resource text not null,
            parent text,
            data jsonb not null,
            created_at timestamptz not null default now(),
            updated_at timestamptz not null default now(),
            unique (organization_id, account_id, resource),
            foreign key (organization_id, account_id) references public.marketing_ad_accounts(organization_id, id) on delete cascade
        )', entry.table_name);
        if entry.parent_table is not null then
            execute format('alter table public.%I add foreign key (organization_id, account_id, parent)
                references public.%I(organization_id, account_id, resource)', entry.table_name, entry.parent_table);
        end if;
    end loop;
end;
$$;

create table public.marketing_ad_assets (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null,
    account_id uuid not null,
    resource text not null,
    kind text not null check (kind in ('headlines', 'descriptions')),
    position int not null check (position >= 0),
    data jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, account_id, resource, kind, position),
    foreign key (organization_id, account_id, resource) references public.marketing_ads(organization_id, account_id, resource) on delete cascade
);

create table public.marketing_performance_daily (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null,
    account_id uuid not null,
    kind text not null check (kind in ('campaigns', 'keywords', 'search_terms')),
    resource text not null,
    day date not null,
    currency text not null,
    spend numeric not null,
    impressions bigint not null,
    clicks bigint not null,
    interactions bigint not null,
    conversions double precision not null,
    value double precision,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, account_id, kind, resource, day, currency),
    foreign key (organization_id, account_id) references public.marketing_ad_accounts(organization_id, id) on delete cascade
);
create index marketing_performance_window on public.marketing_performance_daily(organization_id, account_id, kind, day);

create table public.marketing_draft_revisions (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null,
    account_id uuid not null,
    resource text not null,
    created_by uuid references auth.users(id) on delete set null,
    creative jsonb not null,
    version int not null default 1,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, account_id, resource),
    unique (organization_id, id),
    foreign key (organization_id, account_id, resource) references public.marketing_ads(organization_id, account_id, resource) on delete cascade
);

create table public.marketing_draft_assets (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null,
    draft_id uuid not null,
    kind text not null check (kind in ('headlines', 'descriptions')),
    position int not null check (position >= 0),
    data jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, draft_id, kind, position),
    foreign key (organization_id, draft_id) references public.marketing_draft_revisions(organization_id, id) on delete cascade
);

create table public.marketing_sync_runs (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null,
    account_id uuid not null,
    status text not null check (status in ('running', 'succeeded', 'failed')),
    counts jsonb not null default '{}',
    error text,
    finished_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    foreign key (organization_id, account_id) references public.marketing_ad_accounts(organization_id, id) on delete cascade
);
create index marketing_sync_runs_recent on public.marketing_sync_runs(organization_id, account_id, created_at desc);

create schema if not exists marketing_private;
revoke all on schema marketing_private from public;

create function marketing_private.is_member(org uuid) returns boolean
language sql stable security definer set search_path = ''
as $$
    select exists (select 1 from public.marketing_memberships
        where organization_id = org and user_id = (select auth.uid()))
$$;
revoke all on function marketing_private.is_member(uuid) from public;
grant execute on function marketing_private.is_member(uuid) to authenticated;
grant usage on schema marketing_private to authenticated;

create function marketing_private.touch() returns trigger
language plpgsql set search_path = ''
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

do $$
declare
    t text;
begin
    foreach t in array array['marketing_organizations', 'marketing_memberships', 'marketing_platform_connections', 'marketing_ad_accounts',
        'marketing_campaigns', 'marketing_ad_groups', 'marketing_ads', 'marketing_ad_assets', 'marketing_keywords', 'marketing_search_terms',
        'marketing_performance_daily', 'marketing_draft_revisions', 'marketing_draft_assets', 'marketing_sync_runs']
    loop
        execute format('alter table public.%I enable row level security', t);
        execute format('revoke all on public.%I from public, anon, authenticated', t);
        if t <> 'marketing_platform_connections' then
            execute format('grant select on public.%I to authenticated', t);
            execute format('create policy tenant_read on public.%I for select to authenticated
                using (marketing_private.is_member(%I))', t,
                case when t = 'marketing_organizations' then 'id' else 'organization_id' end);
        end if;
        execute format('create trigger touch_updated_at before update on public.%I
            for each row execute function marketing_private.touch()', t);
    end loop;
end;
$$;
commit;
