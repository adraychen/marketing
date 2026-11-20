"""Reddit uses shared paid metadata and independent nullable daily observations."""
import os
from datetime import date, datetime, timedelta, timezone

import httpx
from psycopg2.extras import Json, execute_values

from backend import content_repository
from backend.repositories import database
from backend.reddit_auth import configuration, access_token
from backend.connectors.reddit_ads import Client, RedditError, fetch_account

PLATFORM = "Reddit Ads"
POLICY = ("Reddit Ads API v3; daily ad metrics in UTC and account currency. Current day is provisional. "
          "Reddit attribution uses the recorded account click/view windows and attribution type, separately from GA4 and Shopify. "
          "Purchases are the selected Reddit purchase attribution, never a sum of different event types. "
          "Reach and frequency are daily ad observations only and are not summed across ads or dates. "
          "Breakdown reports are alternative views, never added to the base daily totals. "
          "Zeros without event-tracking evidence are unavailable. Missing dates are not filled with zero.")


class SyncBusy(Exception):
    pass


def history_start(today):
    try:
        boundary = today.replace(year=today.year - 2)
    except ValueError:  # February 29 in a non-leap target year.
        boundary = today.replace(year=today.year - 2, day=28)
    return boundary + timedelta(days=1)


def status(org):
    with database() as cur:
        cur.execute("select 1 from marketing_paid_observations limit 0")
        cur.execute("select 1 from marketing_private.reddit_tokens where organization_id=%s", (org,))
        authorized = bool(cur.fetchone())
        cur.execute("""select a.id,a.resource,a.data,a.updated_at from marketing_ad_accounts a
            join marketing_platform_connections c on c.id=a.connection_id and c.organization_id=a.organization_id
            where a.organization_id=%s and c.platform=%s order by a.data->>'name'""", (org, PLATFORM))
        accounts = cur.fetchall()
        cur.execute("""select status,message,records,finished_at from marketing_content_sync_runs
            where organization_id=%s and source='reddit_ads' order by finished_at desc limit 10""", (org,))
        runs = cur.fetchall()
    return {"configured": bool(configuration(org)), "authorized": authorized, "schema_ready": True, "accounts": accounts, "runs": runs}


def merge_coverage(old, start, end):
    days = set(old)
    while start <= end:
        days.add(str(start))
        start += timedelta(days=1)
    return sorted(days)


def write_account(cur, org, batch):
    account = batch["account"]
    cur.execute("""insert into marketing_platform_connections(organization_id,platform,credential_ref)
        values(%s,%s,'marketing_private.reddit_tokens') on conflict(organization_id,platform)
        do update set updated_at=now() returning id""", (org, PLATFORM))
    connection = cur.fetchone()["id"]
    cur.execute("select id,data from marketing_ad_accounts where organization_id=%s and connection_id=%s and resource=%s",
                (org, connection, account["id"]))
    previous = cur.fetchone()
    old = previous["data"] if previous else {}
    attribution = batch["attribution"]
    if previous and (old.get("currency") != account["currency"] or old.get("attribution") != attribution):
        # Historical rows keep their own provenance; mixed-definition totals are blocked at read time.
        old_days = []
    else:
        old_days = old.get("coverage_days", [])
    data = {"platform": PLATFORM, "name": account.get("name") or account["id"], "currency": account["currency"],
            "timezone": "UTC", "account_timezone": account.get("time_zone_id"), "attribution": attribution,
            "coverage_days": merge_coverage(old_days, date.fromisoformat(batch["start"]), date.fromisoformat(batch["end"])),
            "source": batch["source"], "limitations": batch["limitations"], "known_events": batch["known_events"],
            "synced_at": datetime.now(timezone.utc).isoformat()}
    cur.execute("""insert into marketing_ad_accounts(organization_id,connection_id,resource,data,synced_from,synced_through)
        values(%s,%s,%s,%s,%s,%s) on conflict(connection_id,resource) do update set data=excluded.data,
        synced_from=excluded.synced_from,synced_through=excluded.synced_through,updated_at=now() returning id""",
        (org, connection, account["id"], Json(data), data["coverage_days"][0], data["coverage_days"][-1]))
    account_id = cur.fetchone()["id"]
    for kind in ("campaigns", "ad_groups", "ads"):
        for row in batch[kind]:
            parent = row.get("campaign_id") if kind == "ad_groups" else row.get("ad_group_id") if kind == "ads" else None
            # Table name is a fixed internal enum, never user input.
            cur.execute(f"""insert into marketing_{kind}(organization_id,account_id,resource,parent,data)
                values(%s,%s,%s,%s,%s) on conflict(organization_id,account_id,resource)
                do update set parent=excluded.parent,data=excluded.data,updated_at=now()""",
                (org, account_id, row["id"], parent, Json(row)))
    # Replace only a successfully fetched window, atomically; older history survives.
    cur.execute("delete from marketing_paid_observations where organization_id=%s and account_id=%s and day between %s and %s",
                (org, account_id, batch["start"], batch["end"]))
    if batch["observations"]:
        execute_values(cur, """insert into marketing_paid_observations
            (organization_id,account_id,platform,day,ad_id,ad_group_id,campaign_id,breakdown,segment,currency,data)
            values %s on conflict(organization_id,account_id,day,ad_id,breakdown,segment)
            do update set data=excluded.data,currency=excluded.currency,synced_at=now()""", [
            (org, account_id, PLATFORM, row["day"], row["ad_id"], row["ad_group_id"], row["campaign_id"],
             row["breakdown"], row["segment"], row["currency"], Json({**row, "attribution": attribution, "timezone": "UTC"}))
            for row in batch["observations"]])
    return account_id


def run_sync(org):
    config = configuration(org)
    if not config:
        raise RedditError("Configure Reddit Ads backend secrets first.")
    try:
        with database() as cur:
            cur.execute("select pg_try_advisory_xact_lock(hashtextextended(%s,0)) as acquired", (f"reddit-ads:{org}",))
            if not cur.fetchone()["acquired"]:
                raise SyncBusy()
            cur.execute("select 1 from marketing_paid_observations limit 0")
            cur.execute("""select a.resource,a.data from marketing_ad_accounts a join marketing_platform_connections c
                on c.id=a.connection_id where a.organization_id=%s and c.platform=%s""", (org, PLATFORM))
            existing = {row["resource"]: row["data"] for row in cur.fetchall()}
            lookback = int(os.getenv("REDDIT_ADS_LOOKBACK_DAYS", "35"))
            if not 31 <= lookback <= 730:
                raise RedditError("REDDIT_ADS_LOOKBACK_DAYS must be 31–730 to cover delayed attribution.")
            extras = tuple(filter(None, (v.strip().upper() for v in os.getenv("REDDIT_ADS_BREAKDOWNS", "PLACEMENT,OS_TYPE,COUNTRY").split(","))))
            today = datetime.now(timezone.utc).date()
            earliest = history_start(today)
            count, accounts = 0, 0
            with httpx.Client(timeout=60) as http:
                client = Client(http, lambda force: access_token(org, force), config["REDDIT_ADS_USER_AGENT"])
                discovered = client.accounts()
                if not discovered:
                    raise RedditError("No accessible Reddit advertising accounts were returned. Check business membership.")
                for account in discovered:
                    old = existing.get(account["id"], {})
                    old_end = max(old.get("coverage_days", []), default=None)
                    start = max(earliest, today-timedelta(days=lookback-1)) if old_end else earliest
                    if old_end:
                        start = min(start, max(earliest, date.fromisoformat(old_end)))
                    batch = fetch_account(client, account, start, today, extras)
                    write_account(cur, org, batch)
                    count += len(batch["observations"])
                    accounts += 1
        content_repository.sync_status(org, "reddit_ads", "succeeded", count, None)
        return {"accounts": accounts, "observations": count}
    except (RedditError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, RedditError) else "Reddit sync failed validation; previous import retained."
        content_repository.sync_status(org, "reddit_ads", "failed", 0, message)
        raise RedditError(message) from None


def report(org, start, end):
    result = []
    with database() as cur:
        cur.execute("""select a.id,a.resource,a.data from marketing_ad_accounts a join marketing_platform_connections c
            on c.id=a.connection_id where a.organization_id=%s and c.platform=%s order by a.resource""", (org, PLATFORM))
        for account in cur.fetchall():
            cur.execute("""select data from marketing_paid_observations where organization_id=%s and account_id=%s
                and day between %s and %s order by day,ad_id,breakdown,segment""", (org, account["id"], start, end))
            rows = [r["data"] for r in cur.fetchall()]
            compatible = all(r["attribution"] == account["data"]["attribution"] and r["currency"] == account["data"]["currency"] for r in rows)
            metadata = {}
            for kind in ("campaigns", "ad_groups", "ads"):
                cur.execute(f"select data from marketing_{kind} where organization_id=%s and account_id=%s order by resource", (org, account["id"]))
                metadata[kind] = [r["data"] for r in cur.fetchall()]
            days = {str(start+timedelta(days=i)) for i in range((end-start).days+1)}
            result.append({"account": account, **metadata, "daily": [r for r in rows if r["breakdown"] == "base"],
                "breakdowns": [r for r in rows if r["breakdown"] != "base"], "compatible": compatible,
                "coverage": days.issubset(set(account["data"].get("coverage_days", []))),
                "stale": datetime.now(timezone.utc)-datetime.fromisoformat(account["data"]["synced_at"]) > timedelta(hours=12)})
    return {"accounts": result, "policy": POLICY, "start": str(start), "end": str(end)}
