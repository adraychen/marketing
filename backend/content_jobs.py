import argparse
import os
import re
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from psycopg2.extras import Json

from backend import commerce, meta_ads, content_repository as repo
from backend.connectors.meta_ads import MetaAdsError
from psycopg2.errors import UndefinedTable
from backend.facebook_analytics import collect_page_metrics, unsupported_page_metrics, write_page_metrics
from backend.config import MODE
from backend.connectors.content import (
    ContentError, analytics_reports, facebook_metrics, facebook_posts, facebook_video_reference, inspect_article,
    response_data, search_article_url, search_property_sample, search_reports, shopify_articles, shopify_token,
)
from backend.repositories import database

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
    is_ga4 = source in ("ga4", "ga4_traffic")
    if is_ga4:
        if not re.fullmatch(r"[0-9]+", source_account):
            return finish(org, source, "failed", message="Set GA4_PROPERTY_ID to a numeric property ID.")
        timezone = environment("CONTENT_GA4_TIMEZONE", "America/Toronto")
        refresh_days, lag = 14, 1
        reports = ["ga4_traffic"] if source == "ga4_traffic" else ["ga4_views", "ga4_landing", "ga4_facebook"]
    else:
        timezone = "America/Los_Angeles"
        refresh_days, lag = 7, 3
        reports = ["gsc_page", "gsc_query"]

    end = datetime.now(ZoneInfo(timezone)).date() - timedelta(days=lag)
    with database() as cur:
        articles = repo.read_items(cur, org, "shopify", shop)
    if not articles:
        return finish(org, source, "skipped", message="Discover published Shopify articles first.")
    records, failures, successful, first_error = 0, 0, 0, None
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
            publication = article["published_at"].astimezone(ZoneInfo(timezone)).date()
            start = max(publication - timedelta(days=7 if is_ga4 else 0), earliest)
            if start > end:
                continue
            if is_ga4:
                rows = list(analytics_reports(client, source_account, article["url"], start, end, utms, kinds=reports))
            else:
                data = article["data"]
                google_url = (data.get("indexing") or {}).get("googleCanonical")
                declared_url = search_article_url(source_account, article["url"], data.get("canonical_url"))
                url = search_article_url(source_account, declared_url, google_url)
                rows = list(search_reports(client, source_account, url, start, end))
            with database() as cur:
                repo.write_daily(cur, org, article["id"], source_account, reports, start, end, rows)
            records += len(rows)
            successful += 1
        except SOURCE_ERRORS as exc:
            failures += 1
            first_error = first_error or message(exc)
    status = "succeeded" if not failures else "partial" if successful else "failed"
    if status == "succeeded" and not successful:
        return finish(org, source, "skipped", message=f"Data not yet available. No eligible article dates through {end}.")
    if status == "succeeded" and not records:
        first_error = f"No matching rows for {successful} articles through {end} ({timezone})."
    return finish(org, source, status, records, first_error)


def article_indexing(client, org, site, shop):
    source = "search_console_inspection"
    if not site or not shop:
        finish(org, source, "skipped", message="Source property or Shopify account is missing.")
        return
    with database() as cur:
        articles = repo.read_items(cur, org, "shopify", shop)
    verified, unavailable = 0, set()
    for article in articles:
        result = inspect_article(client, site, article["url"])
        verified += result["status"] in ("Indexed", "Not indexed")
        if result["status"] not in ("Indexed", "Not indexed"):
            unavailable.add(result["status"])
        with database() as cur:
            cur.execute("""
                update marketing_content_items
                set data = jsonb_set(data, '{indexing}', %s, true)
                where organization_id=%s and id=%s
            """, (Json(result), org, article["id"]))
    status = "skipped" if not articles else "succeeded" if not unavailable else "partial" if verified else "failed"
    detail = ", ".join(sorted(unavailable)) if unavailable else "No articles discovered." if not articles else None
    finish(org, source, status, verified, detail)


def post_insights(client, org, page, token, version):
    if not page or not token or not version:
        return finish(org, "facebook_insights", "skipped", message="Facebook source is not configured.")
    today = datetime.now(ZoneInfo(environment("CONTENT_SNAPSHOT_TIMEZONE", "America/Toronto"))).date()
    with database() as cur:
        posts = repo.read_items(cur, org, "facebook", page)
    if not posts:
        return finish(org, "facebook_insights", "skipped", message="No published posts discovered.")
    records, failures, first_error = 0, 0, None
    restricted_fields = set()
    for post in posts:
        try:
            availability = {}
            _, permalink_id = facebook_video_reference(post["url"])
            metrics = facebook_metrics(client, post["external_id"], token, version,
                                       availability=availability, video_id=post["data"].get("video_id") or permalink_id)
            optional_restrictions = {
                key for key in ("reactions", "comments")
                if metrics and availability.get(key) == "Permission unavailable"
            }
            restricted_fields.update(optional_restrictions)
            errors = {"Authentication failed", "Permission unavailable", "API request failure", "Rate limited"} & {
                state for key, state in availability.items()
                if key not in metrics and key not in optional_restrictions
            }
            if errors:
                failures += 1
                first_error = first_error or ", ".join(sorted(errors))
            with database() as cur:
                repo.write_snapshot(cur, org, post["id"], today, version, {**metrics, "availability": availability})
            records += bool(metrics)
            if "Rate limited" in errors or "Authentication failed" in errors:
                break
        except SOURCE_ERRORS as exc:
            failures += 1
            first_error = first_error or message(exc)
    status = "succeeded" if not failures else "partial" if records else "failed"
    if not failures and not records:
        first_error = "No reportable Meta metrics returned. See per-metric availability."
    if restricted_fields:
        fields = ", ".join(sorted(restricted_fields))
        warning = f"Permission unavailable for optional fields: {fields}."
        first_error = f"{first_error} {warning}" if first_error else warning
    return finish(org, "facebook_insights", status, records, first_error)


def page_insights(client, org, page, token, version, days):
    source = "facebook_page_insights"
    if not page or not token:
        return finish(org, source, "skipped", message="Facebook source is not configured.")
    today = datetime.now(ZoneInfo(environment("CONTENT_SNAPSHOT_TIMEZONE", "America/Toronto"))).date()
    end = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)
    skip = unsupported_page_metrics(org, page, version)
    rows = collect_page_metrics(client, page, token, version, end - timedelta(days=days - 1), end, today, skip=skip)
    write_page_metrics(org, page, version, rows)
    count = sum(row[2] != "status" for row in rows)
    errors = {"Authentication failed", "Permission unavailable", "API request failure", "Rate limited"}
    failures = {row[-1] for row in rows} & errors
    status = ("partial" if count else "failed") if failures else "succeeded"
    return finish(org, source, status, count, ", ".join(sorted(failures)) or None)


def diagnose(client, org, shop, page, token, version):
    site = environment("CONTENT_GSC_SITE")
    property_id = environment("CONTENT_GA4_PROPERTY_ID")
    print("Content visibility diagnostics (read-only)", flush=True)
    with database() as cur:
        articles = repo.read_items(cur, org, "shopify", shop) if shop else []
        posts = repo.read_items(cur, org, "facebook", page) if page else []
        cur.execute("""
            select exists (
                select 1 from pg_constraint
                where conrelid = 'public.marketing_content_daily_metrics'::regclass
                  and pg_get_constraintdef(oid) like %s
            ) as ready
        """, ("%ga4_traffic%",))
        print(f"Content visibility migration accepts GA4 traffic rows: {cur.fetchone()['ready']}", flush=True)
    search_end = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=3)
    search_start = search_end - timedelta(days=89)
    if site:
        try:
            sample = search_property_sample(client, site, search_start, search_end)
            if sample:
                print(f"Search Console property sample ({search_start} to {search_end}): "
                      f"{sample['keys'][0]}, {sample['impressions']} impressions, {sample['clicks']} clicks")
            else:
                print(f"Search Console property sample ({search_start} to {search_end}): no finalized page rows")
        except SOURCE_ERRORS as exc:
            print(f"Search Console property sample: {message(exc)}")
    else:
        print("Search Console: property not configured.")
    for article in articles:
        url = article["url"]
        if site:
            inspection = inspect_article(client, site, url)
            print(f"URL Inspection {url}: {inspection['status']} "
                  f"(HTTP {inspection.get('http_status', 'not reported')})")
            declared = search_article_url(site, url, article["data"].get("canonical_url"))
            canonical = search_article_url(site, declared, inspection.get("googleCanonical"))
            try:
                rows = list(search_reports(client, site, canonical, search_start, search_end))
                print(f"Search Analytics {canonical} through {search_end}: "
                      f"{sum(row['report'] == 'gsc_page' for row in rows)} page rows, "
                      f"{sum(row['report'] == 'gsc_query' for row in rows)} query rows")
            except SOURCE_ERRORS as exc:
                print(f"Search Analytics {canonical}: {message(exc)}")
        if property_id.isascii() and property_id.isdecimal():
            end = datetime.now(ZoneInfo(environment("CONTENT_GA4_TIMEZONE", "America/Toronto"))).date() - timedelta(days=1)
            try:
                rows = list(analytics_reports(client, property_id, url, end - timedelta(days=29), end, [],
                                              kinds={"ga4_views", "ga4_traffic"}))
                scopes = [row["report"] if not row["segment"] else row["segment"] for row in rows]
                print(f"GA4 {url}: {scopes.count('ga4_views')} daily readership rows, "
                      f"{sum('article_views' in scope for scope in scopes)} page-view source rows, "
                      f"{sum('landing_sessions' in scope for scope in scopes)} landing-session source rows")
            except SOURCE_ERRORS as exc:
                print(f"GA4 {url}: {message(exc)}")
    if page and token:
        try:
            identity = response_data(client.get(f"https://graph.facebook.com/{version}/me",
                                               headers={"Authorization": f"Bearer {token}"}, params={"fields": "id"}))
            print(f"Facebook token resolves to configured Page: {identity.get('id') == page}")
        except SOURCE_ERRORS as exc:
            print(f"Facebook Page identity check: {message(exc)}")
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        rows = collect_page_metrics(client, page, token, version, today - timedelta(days=7),
                                    today - timedelta(days=1), today)
        print("Facebook Page metric availability:", {row[0]: row[-1] for row in rows if row[2] == "status"})
        for post in posts:
            kind, permalink_id = facebook_video_reference(post["url"])
            availability = {}
            metrics = facebook_metrics(client, post["external_id"], token, version, availability=availability,
                                       video_id=post["data"].get("video_id") or permalink_id)
            print(f"Facebook {post['external_id']} ({kind or post['data'].get('content_type', 'unknown')}): "
                  f"returned {', '.join(sorted(metrics)) or 'no metrics'}; availability {availability}")
    else:
        print("Facebook: Page ID or token not configured.")
    if environment("META_ADS_ORGANIZATION_ID") == org:
        from backend.connectors.meta_ads import Reader, configuration
        try:
            config = configuration(org)
            if config:
                account = Reader(client,config).get(f"act_{config['account_id']}",{"fields":"account_id,name,currency,timezone_name"})
                print(f"Meta paid account access verified: {account.get('account_id') == config['account_id']}")
                print("Meta paid token expiry:",config['token_expires_at'] or 'Unknown (check Meta debugger)')
            else:
                print("Meta paid: configuration incomplete.")
        except (MetaAdsError,httpx.HTTPError,KeyError,TypeError,ValueError) as exc:
            print("Meta paid:",str(exc) if isinstance(exc,MetaAdsError) else "Account access check failed.")
    return 0


def commerce_sync(client, org, shop, token, client_id, client_secret, days):
    failed = False
    if not environment("COMMERCE_SYNC_ENABLED").lower() in {"1", "true", "yes"}:
        return False
    for source in ("shopify_orders", "ga4_store"):
        try:
            if source == "shopify_orders":
                if not shop or not (token or client_id and client_secret):
                    failed |= finish(org, source, "skipped", message="Shopify store or credentials are missing.")
                    continue
                credential = token or shopify_token(client, shop, client_id, client_secret)
                count = commerce.import_orders(client, org, shop, credential, days)
            else:
                prop = environment("CONTENT_GA4_PROPERTY_ID")
                if not re.fullmatch(r"[0-9]+", prop):
                    failed |= finish(org, source, "skipped", message="Set the numeric GA4 property ID.")
                    continue
                count = commerce.import_sessions(client, org, prop, environment("CONTENT_GA4_TIMEZONE", "America/Toronto"), days)
            finish(org, source, "succeeded", count)
        except UndefinedTable:
            failed |= finish(org, source, "failed", message="Apply the commerce metrics migration before enabling commerce sync.")
        except SOURCE_ERRORS as exc:
            failed |= finish(org, source, "failed", message=message(exc))
    return failed


def main():
    parser = argparse.ArgumentParser(description="Discover and refresh content marketing data.")
    parser.add_argument("--backfill", action="store_true", help="Refresh the entire configured lookback window.")
    parser.add_argument("--diagnose", action="store_true", help="Probe configured content sources without writing data.")
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
    if args.diagnose:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            return diagnose(client, org, shop, page, meta_token, version)
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
        failed |= report_items(client, org, "ga4_traffic", environment("CONTENT_GA4_PROPERTY_ID"), shop, days, args.backfill)
        article_indexing(client, org, environment("CONTENT_GSC_SITE"), shop)
        failed |= report_items(client, org, "search_console", environment("CONTENT_GSC_SITE"), shop, days, args.backfill)
        failed |= post_insights(client, org, page, meta_token, version)
        failed |= page_insights(client, org, page, meta_token, version, days if args.backfill else 28)
        failed |= commerce_sync(client, org, shop, shop_token, shop_client_id, shop_client_secret, days)
    if environment("META_ADS_ORGANIZATION_ID") == org:
        try:
            counts = meta_ads.run_sync(org,int(environment("META_ADS_LOOKBACK_DAYS","90")))
            print(f"meta_ads: succeeded. {counts}",flush=True)
        except meta_ads.SyncBusy:
            print("meta_ads: skipped. A paid-ads sync is already running.",flush=True)
        except UndefinedTable:
            print("meta_ads: failed. Apply 20261111000200_meta_paid_ads.sql.",flush=True)
            failed = True
        except (MetaAdsError,ValueError) as exc:
            text = str(exc) if isinstance(exc,MetaAdsError) else "Check META_ADS_LOOKBACK_DAYS (1-365)."
            print(f"meta_ads: failed. {text}",flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
