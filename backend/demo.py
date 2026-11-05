import json
import os
import random
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from backend.models import Account, Asset, Creative, Daily, ImportBatch, Resource

ORG = "demo"
ACCOUNTS = {
    "demo-ca": Account(resource="demo-ca", name="Okoo Canada", currency="CAD", timezone="America/Toronto"),
    "demo-us": Account(resource="demo-us", name="Okoo United States", currency="USD", timezone="America/New_York"),
}
CAMPAIGNS = (
    ("Brand · Okoo", "SEARCH", "ENABLED", 34),
    ("Camera drones · Non-brand", "SEARCH", "ENABLED", 110),
    ("Drone accessories", "SEARCH", "PAUSED", 28),
    ("Shop all · Performance Max", "PERFORMANCE_MAX", "ENABLED", 150),
)
CREATIVES = (
    (["Shop Okoo Drones", "Find Your Next Camera Drone", "Explore The Okoo Store", "Gear For Your Next Flight"],
     ["Explore camera drones and flight gear at the Okoo store.",
      "Compare features and find the right drone for your next adventure."]),
    (["Shop Camera Drones", "See More From Above", "Find Your First Drone", "Explore Flight Gear"],
     ["Capture a new perspective with camera drones from the Okoo store.",
      "Compare drones for travel, outdoor photography, and everyday flying."]),
    (["Shop Drone Accessories", "Find Spare Drone Parts", "Upgrade Your Flight Kit", "Explore Drone Gear"],
     ["Find accessories for your drone, from spare parts to travel gear.",
      "Build a flight kit that suits the way you like to fly."]),
)
KEYWORDS = (("okoo drones", "EXACT"), ("camera drone", "PHRASE"), ("drone accessories", "PHRASE"))
TERMS = ("okoo camera drones", "camera drone for travel", "spare drone accessories")


def today(account):
    return datetime.now(ZoneInfo(account.timezone)).date()


def accounts():
    return [{**account.model_dump(), "id": key, "synced_from": today(account) - timedelta(days=180),
             "synced_through": today(account) - timedelta(days=1)} for key, account in ACCOUNTS.items()]


def snapshot(account_id, start, end):
    account = ACCOUNTS[account_id]
    local_today = today(account)
    batch = ImportBatch()
    for i, (name, kind, status, budget) in enumerate(CAMPAIGNS):
        campaign = f"{account_id}/campaign/{i + 1}"
        batch.campaigns.append(Resource(resource=campaign, data={
            "id": f"DEMO-{i + 1}", "name": name, "status": status, "type": kind, "daily_budget": budget,
        }))
        group, ad, kw, term = (f"{campaign}/{key}" for key in ("group", "ad", "keyword", "term"))
        if kind == "SEARCH":
            batch.ad_groups.append(Resource(resource=group, parent=campaign, data={
                "name": name.split(" · ")[0], "status": status,
            }))
            headlines, descriptions = CREATIVES[i]
            creative = Creative(
                headlines=[Asset(text=text, pin="HEADLINE_1" if j == 0 else "")
                           for j, text in enumerate(headlines)],
                descriptions=[Asset(text=text) for text in descriptions],
                final_urls=[f"https://example.com/collections/{('drones', 'camera-drones', 'accessories')[i]}"],
                path1="shop", path2=("drones", "camera-drones", "accessories")[i],
            )
            batch.ads.append(Resource(resource=ad, parent=group, data={
                "id": f"DEMO-AD-{i + 1}", "type": "RESPONSIVE_SEARCH_AD", "status": status,
                "strength": ("EXCELLENT", "GOOD", "AVERAGE")[i], "creative": creative.model_dump(),
            }))
            text, match = KEYWORDS[i]
            batch.keywords.append(Resource(resource=kw, parent=group,
                                           data={"text": text, "match_type": match, "status": status}))
            batch.search_terms.append(Resource(resource=term, parent=group,
                                               data={"text": TERMS[i], "status": "NONE"}))
        for offset in range(max(1, (local_today - end).days), min(180, (local_today - start).days) + 1):
            if status == "PAUSED" and offset < 12:
                continue
            day = local_today - timedelta(days=offset)
            rng = random.Random(f"{account_id}:{day}:{i}")
            impressions = int(rng.uniform(0.55, 1.35) * budget * (35 + i * 13))
            clicks = int(impressions * rng.uniform(0.018, 0.038))
            spend = round(budget * rng.uniform(0.55, 0.98), 2)
            conversions = max(0, round(clicks * rng.uniform(0.008, 0.058) - 0.5))
            value = round(conversions * rng.uniform(55, 148), 2)
            for scope, key, portion in (("campaigns", campaign, 1), ("keywords", kw, .54),
                                        ("search_terms", term, .25)):
                if scope != "campaigns" and kind != "SEARCH":
                    continue
                batch.performance.append(Daily(kind=scope, resource=key, day=day, currency=account.currency,
                    spend=round(spend * portion, 2), impressions=int(impressions * portion),
                    clicks=int(clicks * portion), interactions=int(clicks * portion),
                    conversions=round(conversions * portion, 2), value=round(value * portion, 2)))
    return batch.model_dump()


def connection():
    conn = sqlite3.connect(os.getenv("DEMO_DB", "demo.sqlite3"), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""create table if not exists drafts (
        account text, resource text, creative text, version integer,
        updated_at text default current_timestamp, primary key (account, resource)
    )""")
    return conn


def drafts(account_id):
    with closing(connection()) as conn:
        return [{**dict(row), "creative": json.loads(row["creative"])} for row in conn.execute(
            "select resource, creative, version, updated_at from drafts where account=? order by updated_at desc",
            (account_id,))]


def save_draft(account_id, resource, creative, version):
    if not any(ad["resource"] == resource for ad in snapshot(account_id, date.min, date.min)["ads"]):
        raise LookupError
    with closing(connection()) as conn, conn:
        if version is None:
            changed = conn.execute("insert or ignore into drafts (account,resource,creative,version) values (?,?,?,1)",
                                   (account_id, resource, json.dumps(creative))).rowcount
        else:
            changed = conn.execute("""update drafts set creative=?,version=version+1,updated_at=current_timestamp
                where account=? and resource=? and version=?""",
                (json.dumps(creative), account_id, resource, version)).rowcount
        if not changed:
            raise ValueError
        return 1 if version is None else version + 1


def discard_draft(account_id, resource, version):
    with closing(connection()) as conn, conn:
        return bool(conn.execute("delete from drafts where account=? and resource=? and version=?",
                                 (account_id, resource, version)).rowcount)
