alter table public.marketing_content_daily_metrics
    drop constraint if exists marketing_content_daily_metrics_report_check;

alter table public.marketing_content_daily_metrics
    add constraint marketing_content_daily_metrics_report_check
    check (report in ('ga4_views', 'ga4_landing', 'ga4_facebook', 'ga4_traffic', 'gsc_page', 'gsc_query'));

alter table public.marketing_content_metric_snapshots
    drop constraint marketing_content_metric_snapshots_pkey,
    add primary key (organization_id, item_id, observed_day, api_version, observed_at);
