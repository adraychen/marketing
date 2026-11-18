"""GA4 session-scoped attribution evidence, independent of canonical orders."""
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from psycopg2.extras import Json, execute_values
from backend.connectors.content import ContentError, analytics_response, google_headers
from backend.repositories import database
from backend.channels import classify

DIMENSIONS = ("date", "sessionSource", "sessionMedium", "sessionCampaignName", "sessionManualSource",
              "sessionManualMedium", "sessionManualCampaignName", "sessionManualAdContent", "sessionManualTerm")
METRICS = ("sessions", "itemViewEvents", "addToCarts", "checkouts", "ecommercePurchases", "purchaseRevenue")
LABELS = ("sessions", "product_views", "add_to_carts", "checkouts", "ga4_purchases", "ga4_purchase_revenue")


def collect(client, prop, zone, start, end):
    endpoint = f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport"
    headers = google_headers(endpoint, "analytics.readonly")
    body = {"dateRanges": [{"startDate": str(start), "endDate": str(end)}],
            "dimensions": [{"name": key} for key in DIMENSIONS], "metrics": [{"name": key} for key in METRICS],
            "orderBys": [{"dimension": {"dimensionName": key}} for key in DIMENSIONS], "offset": "0", "limit": "10000"}
    rows, seen, currency = [], set(), None
    while True:
        result = analytics_response(client.post(endpoint, headers=headers, json=body))
        meta = result.get("metadata", {})
        if meta.get("timeZone") != zone:
            raise ContentError("GA4 channel report time zone differs from configuration.")
        if (meta.get("subjectToThresholding") or meta.get("dataLossFromOtherRow") or meta.get("samplingMetadatas")
                or meta.get("schemaRestrictionResponse", {}).get("activeMetricRestrictions")):
            raise ContentError("GA4 channel report is restricted, sampled or thresholded; prior data retained.")
        currency = meta.get("currencyCode")
        page = result.get("rows", [])
        for row in page:
            dimensions = dict(zip(DIMENSIONS, (v["value"] for v in row["dimensionValues"]), strict=True))
            day = datetime.strptime(dimensions.pop("date"), "%Y%m%d").date()
            key = (day, json.dumps(dimensions, sort_keys=True))
            metrics = dict(zip(LABELS, (float(v["value"]) for v in row["metricValues"]), strict=True))
            if key in seen or not start <= day <= end or any(not math.isfinite(v) or v < 0 for v in metrics.values()):
                raise ContentError("GA4 channel report has duplicate or invalid rows; prior data retained.")
            seen.add(key)
            rows.append({"day": day, "segment": key[1], "dimensions": dimensions, "metrics": metrics})
        offset = int(body["offset"]) + len(page)
        if not page and offset < result.get("rowCount", offset):
            raise ContentError("GA4 channel report pagination is incomplete; prior data retained.")
        if offset >= result.get("rowCount", offset):
            break
        body["offset"] = str(offset)
    return rows, currency


def sync(client, org, prop, zone, days):
    with database() as cur:
        cur.execute("select to_regclass('public.marketing_channel_daily') as present")
        if not cur.fetchone()["present"]:
            raise ContentError("Apply the channel attribution migration before collecting manual UTM evidence.")
    end = datetime.now(ZoneInfo(zone)).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    rows, currency = collect(client, prop, zone, start, end)
    with database() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))", (f"ga4-channels:{org}:{prop}",))
        cur.execute("delete from marketing_channel_daily where organization_id=%s and property_id=%s and day between %s and %s",
                    (org, prop, start, end))
        if rows:
            execute_values(cur, "insert into marketing_channel_daily(organization_id,property_id,day,segment,dimensions,metrics) values %s",
                           [(org, prop, r["day"], r["segment"], Json(r["dimensions"]), Json(r["metrics"])) for r in rows])
        cur.execute("""insert into marketing_commerce_coverage(organization_id,source,account,start_day,end_day,timezone,currency)
                    values(%s,'ga4_channels',%s,%s,%s,%s,%s) on conflict(organization_id,source,account) do update set
                    start_day=excluded.start_day,end_day=excluded.end_day,timezone=excluded.timezone,currency=excluded.currency,collected_at=now()""",
                    (org, prop, start, end, zone, currency))
    return len(rows)


def report_rows(rows, owned, zone="UTC"):
    result = []
    for row in rows:
        dims = row["dimensions"]
        raw = {key: dims.get(field) for key, field in zip(("source", "medium", "campaign", "content", "term"),
               ("sessionManualSource", "sessionManualMedium", "sessionManualCampaignName", "sessionManualAdContent", "sessionManualTerm"))}
        # Session origin classifies traffic; matching owned content also requires manual tags to agree.
        origin = {"source": dims.get("sessionSource"), "medium": dims.get("sessionMedium"), "campaign": dims.get("sessionCampaignName"),
                  "content": raw["content"], "term": raw["term"]}
        registry = owned if all(raw[k] == origin[k] for k in ("source", "medium", "campaign")) else {}
        observed = datetime.combine(row["day"], datetime.min.time(), ZoneInfo(zone))
        result.append({**row, **row["metrics"], **classify(origin, registry, occurred_at=observed), "original_utm": raw,
                       "measurement_source": "GA4 session attribution; not Shopify orders"})
    return result
