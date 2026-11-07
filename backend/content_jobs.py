import argparse
import os
import re
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from psycopg2.extras import Json

from backend import content_repository as repo
from backend.config import MODE
from backend.connectors.content import (
    ContentError, analytics_reports, facebook_metrics, facebook_posts, search_reports, shopify_articles, shopify_token,
)
from backend.repositories import database

SOURCES = ("shopify", "facebook", "ga4", "search_console", "facebook_insights")
SOURCE_ERRORS = (ContentError, httpx.HTTPError, KeyError, TypeError, ValueError)


def environment(name, default=""):
    return os.getenv(name, default).strip()


def finish(org, source, status, records=0, message=None):
    repo.sync_status(org, source, status, records, message)
    print(f"{source}: {status}, {records} records." + (f" {message}" if message else ""), flush=True)
    return status in {"failed", "partial"}


def message(exc):
    return str(exc) if isinstance(exc, ContentError) else "Source request or response failed. Check source configuration."


def discovery(client, org, source, account, token, fetch):
    if not account or not token:
        return finish(org, source, "skipped", message="Source account or credential is missing.")
    try:
        rows = list(fetch(client, account, token))
        with database() as cur:
            repo.write_items(cur, org, source, account, rows)
    except SOURCE_ERRORS as exc:
        return finish(org, source, "failed", message=message(exc))
    return finish(org, source, "succeeded", len(rows))


def latest_day(cur, org, item_id, source_account, report):
    cur.execute("""
        select max(day) as day from marketing_content_daily_metrics
        where organization_id=%s and item_id=%s and source_account=%s and report=%s
    """, (org, item_id, source_account, report))
    return cur.fetchone()["day"]


def report_items(client, org, source, source_account, shop, backfill_days, force):
    if not source_account or not shop:
        return finish(org, source, "skipped", message="Source property or Shopify account is missing.")
    if source == "ga4":
        if not re.fullmatch(r"[0-9]+", source_account):
            return finish(org, source, "failed", message="Set GA4_PROPERTY_ID to a numeric property ID.")
        timezone = environment("CONTENT_GA4_TIMEZONE", "America/Toronto")
        refresh_days, lag = 14, 1
        reports = ["ga4_views", "ga4_landing", "ga4_facebook"]
    else:
        timezone = "America/Los_Angeles"
        refresh_days, lag = 7, 3
        reports = ["gsc_page", "gsc_query"]

    end = datetime.now(ZoneInfo(timezone)).date() - timedelta(days=lag)
    with database() as cur:
        articles = repo.read_items(cur, org, "shopify", shop)
    if not articles:
        return finish(org, source, "skipped", message="Discover published Shopify articles first.")
    records, failures, first_error = 0, 0, None
    for article in articles:
        try:
            with database() as cur:
                latest = latest_day(cur, org, article["id"], source_account, reports[0])
                utms = repo.article_utms(cur, org, article["id"]) if source == "ga4" else []
            if (source == "ga4" and repo.url_key(article["url"])[1] == repo.PILOT_PATH
                    and repo.PILOT_UTM not in utms):
                utms.append(repo.PILOT_UTM)
            earliest = end - timedelta(days=backfill_days - 1)
            if latest and not force:
                earliest = min(end - timedelta(days=refresh_days - 1), latest + timedelta(days=1))
            start = max(article["published_at"].astimezone(ZoneInfo(timezone)).date(), earliest)
            if start > end:
                continue
            if source == "ga4":
                rows = list(analytics_reports(client, source_account, article["url"], start, end, utms))
            else:
                rows = list(search_reports(client, source_account, article["url"], start, end))
            with database() as cur:
                repo.write_daily(cur, org, article["id"], source_account, reports, start, end, rows)
            records += len(rows)
        except SOURCE_ERRORS as exc:
            failures += 1
            first_error = first_error or message(exc)
    status = "succeeded" if not failures else "failed" if failures == len(articles) else "partial"
    return finish(org, source, status, records, first_error)


def post_insights(client, org, page, token, version):
    if not page or not token or not version:
        return finish(org, "facebook_insights", "skipped", message="Facebook source is not configured.")
    today = datetime.now(ZoneInfo(environment("CONTENT_SNAPSHOT_TIMEZONE", "America/Toronto"))).date()
    with database() as cur:
        posts = repo.read_items(cur, org, "facebook", page)
    if not posts:
        return finish(org, "facebook_insights", "skipped", message="No published posts discovered.")
    records, failures, first_error = 0, 0, None
    for post in posts:
        try:
            metrics = facebook_metrics(client, post["external_id"], token, version)
            if not metrics:
                raise ContentError("Meta returned no supported metrics for a post. Check Insights access.")
            with database() as cur:
                cur.execute("""
                    insert into marketing_content_metric_snapshots
                        (organization_id,item_id,observed_day,api_version,metrics) values (%s,%s,%s,%s,%s)
                    on conflict (organization_id,item_id,observed_day,api_version) do update set
                        metrics=excluded.metrics,observed_at=now()
                """, (org, post["id"], today, version, Json(metrics)))
            records += 1
        except SOURCE_ERRORS as exc:
            failures += 1
            first_error = first_error or message(exc)
    status = "succeeded" if not failures else "failed" if failures == len(posts) else "partial"
    return finish(org, "facebook_insights", status, records, first_error)


def main():
    parser = argparse.ArgumentParser(description="Discover and refresh content marketing data.")
    parser.add_argument("--backfill", action="store_true", help="Refresh the entire configured lookback window.")
    args = parser.parse_args()
    try:
        org = str(UUID(environment("CONTENT_ORGANIZATION_ID")))
        days = int(environment("CONTENT_BACKFILL_DAYS", "90"))
        if MODE != "live" or not 14 <= days <= 365:
            raise ValueError
    except ValueError:
        parser.exit(2, "Set live mode, a valid CONTENT_ORGANIZATION_ID and a 14–365 day backfill window.\n")

    shop = environment("CONTENT_SHOPIFY_SHOP")
    storefront = environment("CONTENT_STOREFRONT_URL", "https://okoostore.com")
    blog = environment("CONTENT_SHOPIFY_BLOG", "journal")
    page = environment("CONTENT_META_PAGE_ID")
    version = environment("CONTENT_META_API_VERSION", "v26.0")
    if shop and not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", shop):
        parser.exit(2, "Set CONTENT_SHOPIFY_SHOP to a myshopify.com hostname.\n")
    if page and not re.fullmatch(r"[0-9]+", page):
        parser.exit(2, "Set CONTENT_META_PAGE_ID to a numeric Page ID.\n")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
        parser.exit(2, "Set CONTENT_META_API_VERSION to a version such as v26.0.\n")
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+/?", storefront):
        parser.exit(2, "Set CONTENT_STOREFRONT_URL to an HTTPS store origin.\n")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", blog):
        parser.exit(2, "Set CONTENT_SHOPIFY_BLOG to the blog handle.\n")
    try:
        ZoneInfo(environment("CONTENT_GA4_TIMEZONE", "America/Toronto"))
        ZoneInfo(environment("CONTENT_SNAPSHOT_TIMEZONE", "America/Toronto"))
    except ZoneInfoNotFoundError:
        parser.exit(2, "Set content timezones to valid IANA timezone names.\n")

    shop_token = environment("CONTENT_SHOPIFY_ACCESS_TOKEN")
    shop_client_id = environment("CONTENT_SHOPIFY_CLIENT_ID")
    shop_client_secret = environment("CONTENT_SHOPIFY_CLIENT_SECRET")
    meta_token = environment("CONTENT_META_PAGE_ACCESS_TOKEN")
    with database() as lock, httpx.Client(timeout=30, follow_redirects=False) as client:
        lock.execute("select pg_try_advisory_xact_lock(hashtextextended(%s, 0)) as acquired", (f"content:{org}",))
        if not lock.fetchone()["acquired"]:
            print("A content sync is already running.")
            return 0
        def fetch_articles(c, account, credential):
            token = shop_token or shopify_token(c, account, shop_client_id, credential)
            return shopify_articles(c, account, token, storefront, blog)

        failed = discovery(client, org, "shopify", shop,
                           shop_token or (shop_client_secret if shop_client_id else ""), fetch_articles)
        failed |= discovery(client, org, "facebook", page, meta_token,
                            lambda c, account, token: facebook_posts(c, account, token, version))
        with database() as cur:
            repo.associate(cur, org, shop, page)
        failed |= report_items(client, org, "ga4", environment("CONTENT_GA4_PROPERTY_ID"), shop, days, args.backfill)
        failed |= report_items(client, org, "search_console", environment("CONTENT_GSC_SITE"), shop, days, args.backfill)
        failed |= post_insights(client, org, page, meta_token, version)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
