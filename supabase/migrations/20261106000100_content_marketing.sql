begin;

create table public.marketing_content_campaigns (
    organization_id uuid not null references public.marketing_organizations(id),
    slug text not null,
    name text not null,
    primary key (organization_id, slug)
);

create table public.marketing_content_items (
    organization_id uuid not null references public.marketing_organizations(id),
    id uuid not null default gen_random_uuid(),
    source text not null check (source in ('shopify', 'facebook')),
    source_account text not null,
    external_id text not null,
    title text not null,
    url text,
    published_at timestamptz not null,
    active boolean not null default true,
    data jsonb not null default '{}',
    primary key (organization_id, id),
    unique (organization_id, source, source_account, external_id)
);

create table public.marketing_content_campaign_items (
    organization_id uuid not null,
    campaign_slug text not null,
    item_id uuid not null,
    primary key (organization_id, campaign_slug, item_id),
    foreign key (organization_id, campaign_slug) references public.marketing_content_campaigns(organization_id, slug),
    foreign key (organization_id, item_id) references public.marketing_content_items(organization_id, id)
);

create table public.marketing_content_links (
    organization_id uuid not null,
    post_id uuid not null,
    article_id uuid not null,
    url text not null,
    utm jsonb not null,
    primary key (organization_id, post_id, article_id, url),
    foreign key (organization_id, post_id) references public.marketing_content_items(organization_id, id),
    foreign key (organization_id, article_id) references public.marketing_content_items(organization_id, id)
);

create table public.marketing_content_products (
    organization_id uuid not null,
    campaign_slug text not null,
    shopify_product_gid text not null,
    label text not null,
    primary key (organization_id, campaign_slug, shopify_product_gid),
    foreign key (organization_id, campaign_slug) references public.marketing_content_campaigns(organization_id, slug)
);

create table public.marketing_content_ad_campaigns (
    organization_id uuid not null,
    campaign_slug text not null,
    ad_account_id uuid not null,
    ad_campaign_resource text not null,
    primary key (organization_id, campaign_slug, ad_account_id, ad_campaign_resource),
    foreign key (organization_id, campaign_slug) references public.marketing_content_campaigns(organization_id, slug),
    foreign key (organization_id, ad_account_id, ad_campaign_resource)
        references public.marketing_campaigns(organization_id, account_id, resource)
);

create table public.marketing_content_daily_metrics (
    organization_id uuid not null,
    item_id uuid not null,
    source_account text not null,
    report text not null check (report in ('ga4_views', 'ga4_landing', 'ga4_facebook', 'gsc_page', 'gsc_query')),
    day date not null,
    segment text not null default '',
    timezone text not null,
    currency text,
    metrics jsonb not null,
    collected_at timestamptz not null default now(),
    primary key (organization_id, item_id, report, source_account, day, segment),
    foreign key (organization_id, item_id) references public.marketing_content_items(organization_id, id)
);

create table public.marketing_content_metric_snapshots (
    organization_id uuid not null,
    item_id uuid not null,
    observed_day date not null,
    observed_at timestamptz not null default now(),
    api_version text not null,
    metrics jsonb not null,
    primary key (organization_id, item_id, observed_day, api_version),
    foreign key (organization_id, item_id) references public.marketing_content_items(organization_id, id)
);

create table public.marketing_content_sync_runs (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.marketing_organizations(id),
    source text not null,
    status text not null check (status in ('succeeded', 'failed', 'skipped', 'partial')),
    records integer not null default 0,
    message text,
    finished_at timestamptz not null default now()
);
create index marketing_content_sync_recent
    on public.marketing_content_sync_runs(organization_id, source, finished_at desc);

do $$
declare
    t text;
begin
    foreach t in array array[
        'marketing_content_campaigns', 'marketing_content_items', 'marketing_content_campaign_items',
        'marketing_content_links', 'marketing_content_products', 'marketing_content_ad_campaigns',
        'marketing_content_daily_metrics', 'marketing_content_metric_snapshots', 'marketing_content_sync_runs'
    ]
    loop
        execute format('alter table public.%I enable row level security', t);
        execute format('revoke all on public.%I from public, anon, authenticated', t);
        execute format('grant select on public.%I to authenticated', t);
        execute format('create policy tenant_read on public.%I for select to authenticated
            using (marketing_private.is_member(organization_id))', t);
    end loop;
end;
$$;

commit;
