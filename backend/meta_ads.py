"""Tenant-scoped Meta paid import and reporting. Snapshot replacement is atomic."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from psycopg2.extras import Json, execute_values

from backend.connectors.meta_ads import MetaAdsError, Reader, configuration, fetch
from backend.repositories import database

POLICY = ("Meta paid delivery across all placements in the configured ad account, not Facebook-only placement data. "
          "Daily ad rows use account timezone, ad-set unified attribution, and action_report_time=impression. "
          "Website purchases use only offsite_conversion.fb_pixel_purchase. Missing action categories remain unavailable, "
          "not zero. Raw action categories overlap and must not be summed. Meta conversions are separate from Shopify "
          "orders and Google Ads conversions. Reach and frequency are daily per-ad observations and are never added. "
          "Budgets and bids are raw API minor currency units, not spend amounts, and reflect latest discovery. "
          "Creative data may be incomplete for existing-post, catalog, or dynamically generated ads.")


class SyncBusy(Exception):
    pass


def empty_report():
    return {"account":None, "campaigns":[], "adsets":[], "ads":[], "daily":[], "coverage":False,
            "stale":False, "policy":POLICY}


def status(org):
    config = configuration(org)
    with database() as cur:
        cur.execute("select account_id,data,synced_from,synced_through,synced_at from marketing_meta_ads_accounts where organization_id=%s order by synced_at desc", (org,))
        accounts = cur.fetchall()
        cur.execute("select account_id,status,counts,message,finished_at from marketing_meta_ads_sync_runs where organization_id=%s order by finished_at desc limit 20", (org,))
        runs = cur.fetchall()
    expiry = config["token_expires_at"] if config else None
    days = (datetime.fromisoformat(expiry)-datetime.now(timezone.utc)).total_seconds()/86400 if expiry else None
    return {"configured":bool(config), "account_id":config["account_id"] if config else None,
            "accounts":accounts, "runs":runs, "token_expires_at":expiry,
            "expiry_source":"Operator supplied; not verified by Meta" if expiry else "Unknown; check Meta Access Token Debugger",
            "token_expiring": days is not None and days<=7, "token_expired":days is not None and days<=0}


def record_failure(org, account_id, message):
    with database() as cur:
        cur.execute("insert into marketing_meta_ads_sync_runs(organization_id,account_id,status,message) values(%s,%s,'failed',%s)",
                    (org, account_id, message))


def write_import(cur, org, config, account, resources, metrics, first, end):
    account_id = config["account_id"]
    data = {**account, "api_version":config["version"], "token_expires_at":config["token_expires_at"],
            "expiry_source":"Operator supplied; not verified by Meta", "action_report_time":"impression",
            "use_unified_attribution_setting":True}
    cur.execute("""insert into marketing_meta_ads_accounts(organization_id,account_id,data,synced_from,synced_through)
                   values(%s,%s,%s,%s,%s) on conflict(organization_id,account_id) do update set
                   data=excluded.data,synced_from=excluded.synced_from,synced_through=excluded.synced_through,synced_at=now()""",
                (org,account_id,Json(data),first,end))
    # Retain older metadata. last_seen_at identifies resources absent from latest discovery.
    values = [(org,account_id,kind,row["id"],Json(row)) for kind,rows in resources.items() for row in rows]
    if values:
        execute_values(cur,"""insert into marketing_meta_ads_resources(organization_id,account_id,kind,resource_id,data)
                           values %s on conflict(organization_id,account_id,kind,resource_id) do update set
                           data=excluded.data,last_seen_at=now()""", values)
    cur.execute("delete from marketing_meta_ads_daily where organization_id=%s and account_id=%s and day between %s and %s",
                (org,account_id,first,end))
    if metrics:
        execute_values(cur,"insert into marketing_meta_ads_daily(organization_id,account_id,ad_id,day,campaign_id,adset_id,data) values %s",
                       [(org,account_id,row["ad_id"],row["day"],row["campaign_id"],row["adset_id"],Json(row)) for row in metrics])
    counts = {kind:len(rows) for kind,rows in resources.items()}
    counts["daily_ad_rows"] = len(metrics)
    cur.execute("insert into marketing_meta_ads_sync_runs(organization_id,account_id,status,counts) values(%s,%s,'succeeded',%s)",
                (org,account_id,Json(counts)))
    return counts


def run_sync(org, days=90):
    config = configuration(org)
    if not config:
        raise MetaAdsError("Meta paid ads are not configured for this organization.")
    if not 1<=days<=365:
        raise MetaAdsError("Meta import lookback must be between 1 and 365 days.")
    try:
        with database() as cur:
            cur.execute("select pg_try_advisory_xact_lock(hashtextextended(%s,0)) as acquired", (f"meta-paid:{org}:{config['account_id']}",))
            if not cur.fetchone()["acquired"]:
                raise SyncBusy("A Meta paid-ads sync is already running.")
            cur.execute("select 1 from marketing_meta_ads_accounts limit 0")
            with httpx.Client(timeout=30,follow_redirects=False) as client:
                if config["token_expires_at"] and datetime.fromisoformat(config["token_expires_at"])<=datetime.now(timezone.utc):
                    raise MetaAdsError("The configured paid-ads token expiry is in the past. Renew it in Render.")
                identity = Reader(client,config).get(f"act_{config['account_id']}", {"fields":"account_id,timezone_name"})
                if str(identity.get("account_id")) != config["account_id"]:
                    raise MetaAdsError("Configured Meta ad account does not match the API response.")
                end = datetime.now(ZoneInfo(identity["timezone_name"])).date()-timedelta(days=1)
                first = end-timedelta(days=days-1)
                account,resources,metrics = fetch(client,config,first,end)
            return write_import(cur,org,config,account,resources,metrics,first,end)
    except SyncBusy:
        raise
    except (MetaAdsError,httpx.HTTPError,KeyError,ValueError,TypeError,OverflowError) as exc:
        message = str(exc) if isinstance(exc,MetaAdsError) else "Meta paid-ads request or response failed. Previous import retained."
        record_failure(org,config["account_id"],message)
        raise MetaAdsError(message) from None


def report(org,start,end):
    result = empty_report()
    config = configuration(org)
    with database() as cur:
        cur.execute("select account_id,data,synced_from,synced_through,synced_at from marketing_meta_ads_accounts where organization_id=%s order by synced_at desc", (org,))
        accounts = cur.fetchall()
        account = next((row for row in accounts if not config or row["account_id"]==config["account_id"]),None)
        if not account:
            return result
        account_id = account["account_id"]
        result["account"] = account
        result["coverage"] = account["synced_from"]<=start<=end<=account["synced_through"]
        result["stale"] = datetime.now(timezone.utc)-account["synced_at"]>timedelta(hours=48)
        cur.execute("select kind,resource_id,data,last_seen_at from marketing_meta_ads_resources where organization_id=%s and account_id=%s order by kind,resource_id",(org,account_id))
        for row in cur.fetchall():
            result[row["kind"]].append({**row["data"], "last_seen_at":row["last_seen_at"]})
        cur.execute("select data from marketing_meta_ads_daily where organization_id=%s and account_id=%s and day between %s and %s order by day,ad_id",(org,account_id,start,end))
        result["daily"] = [row["data"] for row in cur.fetchall()]
    # Insights can include inaccessible/deleted resources absent from metadata edges.
    for metric in result["daily"]:
        for kind,identifier,name in (("campaigns","campaign_id","campaign_name"),("adsets","adset_id","adset_name"),("ads","ad_id","ad_name")):
            if not any(row["id"]==metric[identifier] for row in result[kind]):
                data = {"id":metric[identifier], "name":metric.get(name), "metadata_unavailable":True}
                if kind != "campaigns":
                    data["campaign_id"] = metric["campaign_id"]
                if kind == "ads":
                    data["adset_id"] = metric["adset_id"]
                result[kind].append(data)
    return result
