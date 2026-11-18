"""Tenant-scoped persisted ChatGPT Ads reports."""
from datetime import datetime, timedelta, timezone
import os
import httpx
from psycopg2.extras import Json
from backend.connectors.chatgpt_ads import AdsError, configuration, fetch
from backend.repositories import database

POLICY = ("ChatGPT Ads daily ad observations in the ad account time zone and currency. Attribution uses the recorded click/view windows "
          "and ad-interaction date. Goal conversions are not necessarily purchases. Order Created Sales is platform-attributed purchase value, "
          "separate from Shopify canonical orders and GA4. Null/missing values remain unavailable. Delivery and spend can finalize at different "
          "times; conversions update daily. Provider CPC/CPM are retained in daily rows; aggregate ratios are calculated and labeled separately.")


class SyncBusy(Exception):
    pass


def empty_report():
    return {"account": None, "daily": [], "coverage": False, "stale": False, "policy": POLICY, "attribution": {}}


def status(org):
    config = configuration(org)
    with database() as cur:
        cur.execute("select account_id,synced_at,start_day,end_day from marketing_chatgpt_ads_reports where organization_id=%s", (org,))
        accounts = cur.fetchall()
        cur.execute("select status,message,finished_at from marketing_chatgpt_ads_sync_runs where organization_id=%s order by finished_at desc limit 10", (org,))
        runs = cur.fetchall()
    return {"configured": bool(config), "schema_ready": True, "accounts": accounts, "runs": runs}


def run_sync(org):
    config = configuration(org)
    if not config:
        raise AdsError("Set OPENAI_ADS_ORGANIZATION_ID and OPENAI_ADS_API_KEY in the backend and sync-job environment.")
    try:
        with database() as cur:
            cur.execute("select pg_try_advisory_xact_lock(hashtextextended(%s,0)) as acquired", (f"chatgpt-ads:{org}",))
            if not cur.fetchone()["acquired"]:
                raise SyncBusy("ChatGPT Ads sync is already running.")
            cur.execute("select 1 from marketing_chatgpt_ads_reports limit 0")
            with httpx.Client(timeout=45, follow_redirects=False) as client:
                data = fetch(client, config, int(os.getenv("OPENAI_ADS_LOOKBACK_DAYS", "90")))
            cur.execute("""insert into marketing_chatgpt_ads_reports(organization_id,account_id,start_day,end_day,data)
                           values(%s,%s,%s,%s,%s) on conflict(organization_id) do update set
                           account_id=excluded.account_id,start_day=excluded.start_day,end_day=excluded.end_day,
                           data=excluded.data,synced_at=now()""", (org, data["account"]["id"], data["start"], data["end"], Json(data)))
            cur.execute("insert into marketing_chatgpt_ads_sync_runs(organization_id,status) values(%s,'succeeded')", (org,))
        return {"daily_ad_rows": len(data["daily"]), "account_id": data["account"]["id"]}
    except (AdsError, httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        message = str(exc) if isinstance(exc, AdsError) else "ChatGPT Ads request or configuration failed; previous import retained."
        with database() as cur:
            cur.execute("insert into marketing_chatgpt_ads_sync_runs(organization_id,status,message) values(%s,'failed',%s)", (org, message))
        raise AdsError(message) from None


def report(org, start, end):
    with database() as cur:
        cur.execute("select data,synced_at,start_day,end_day from marketing_chatgpt_ads_reports where organization_id=%s", (org,))
        row = cur.fetchone()
    if not row:
        return empty_report()
    data = row["data"]
    return {**data, "daily": [r for r in data["daily"] if str(start) <= r["day"] <= str(end)],
            "coverage": row["start_day"] <= start <= end <= row["end_day"], "synced_at": row["synced_at"],
            "stale": datetime.now(timezone.utc) - row["synced_at"] > timedelta(hours=48), "policy": POLICY}
