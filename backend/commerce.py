from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
from zoneinfo import ZoneInfo

from psycopg2.extras import Json, execute_values

from backend.connectors.content import SHOPIFY_VERSION, ContentError, analytics_response, google_headers, response_data
from backend.repositories import database
from backend.channels import classify, owned_tags, coverage as channel_coverage, comparisons

CONTEXT_QUERY = """
query CommerceContext {
  currentAppInstallation { accessScopes { handle } }
  shop { ianaTimezone currencyCode }
}
"""
ORDERS_QUERY = """
query CommerceOrders($after: String, $query: String!, $details: Boolean!) {
  orders(first: 100, after: $after, query: $query, sortKey: CREATED_AT) {
    nodes {
      id createdAt updatedAt test cancelledAt displayFinancialStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }
      currentTotalTaxSet @include(if: $details) { shopMoney { amount currencyCode } }
      lineItems(first: 100) @include(if: $details) {
        nodes { currentQuantity variant { id } }
        pageInfo { hasNextPage }
      }
      customerJourneySummary {
        ready
        lastVisit { occurredAt utmParameters { source medium campaign content term } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""
POLICY = ("Non-test, non-canceled orders currently PAID or PARTIALLY_REFUNDED, grouped by order creation date "
          "in the Shopify store time zone. Revenue is currentTotalPriceSet.shopMoney, including Shopify's "
          "current tax, shipping, discount, and refund adjustments. Fully refunded orders are excluded.")
ATTRIBUTION = ("Shopify last recorded visit with explicit UTM source and medium. "
               "This is not last non-direct click or proof of causation. Missing or unready journeys remain unattributed.")


def shopify_query(client, shop, token, query, variables=None):
    result = response_data(client.post(f"https://{shop}/admin/api/{SHOPIFY_VERSION}/graphql.json",
                                      headers={"X-Shopify-Access-Token": token},
                                      json={"query": query, "variables": variables or {}}))
    if result.get("errors"):
        codes = {row.get("extensions", {}).get("code") for row in result["errors"]}
        if "ACCESS_DENIED" in codes:
            raise ContentError("Shopify order access denied. Verify read_orders and access to customer journey fields.")
        raise ContentError("Shopify order query failed. Check read scopes, throttling, and API configuration.")
    return result["data"]


def channel_for(utm):
    """Legacy storage label retained for audit; reports use channels.classify instead."""
    source = (utm.get("source") or "").lower()
    medium = (utm.get("medium") or "").lower()
    if source in {"facebook", "fb"} and medium == "organic_social":
        return "Facebook Organic"
    if source in {"google", "google_ads"} and medium in {"cpc", "ppc", "paid_search"}:
        return "Google Ads"
    if source in {"facebook", "fb", "instagram", "meta"} and medium in {"cpc", "ppc", "paid_social"}:
        return "Meta Paid"
    if source == "google" and medium == "organic" or source == "shopify_blog" and medium == "content":
        return "Shopify Blog / Organic Search"
    return "Other / Direct / Unattributed"


def normalize_order(order):
    money = order["currentTotalPriceSet"]["shopMoney"]
    try:
        value = Decimal(money["amount"])
    except (InvalidOperation, TypeError):
        raise ContentError("Shopify returned an invalid order amount.") from None
    if not value.is_finite() or value < 0:
        raise ContentError("Shopify returned an invalid order amount.")
    journey = order.get("customerJourneySummary") or {}
    visit = journey.get("lastVisit") if journey.get("ready") else None
    utm = {key: str(value)[:250] for key, value in ((visit or {}).get("utmParameters") or {}).items() if value}
    attributed = bool(visit and utm.get("source") and utm.get("medium"))
    eligible = not order["test"] and not order["cancelledAt"] and order["displayFinancialStatus"] in {"PAID", "PARTIALLY_REFUNDED"}
    return {"order_id": order["id"], "created_at": order["createdAt"], "updated_at": order["updatedAt"],
            "eligible": bool(eligible), "financial_status": order["displayFinancialStatus"],
            "amount": value, "currency": money["currencyCode"],
            "presentment": order["currentTotalPriceSet"]["presentmentMoney"],
            "channel": channel_for(utm) if attributed else "Other / Direct / Unattributed",
            "attributed": attributed, "utm": utm,
            "evaluation": {"tax": order.get("currentTotalTaxSet", {}).get("shopMoney"),
                           "line_items": order.get("lineItems")}}


def import_orders(client, org, shop, token, days, detailed=False):
    context = shopify_query(client, shop, token, CONTEXT_QUERY)
    scopes = {scope["handle"] for scope in context["currentAppInstallation"]["accessScopes"]}
    if not scopes & {"read_orders", "write_orders"}:
        raise ContentError("Shopify order import needs read_orders access on the installed app.")
    zone = context["shop"]["ianaTimezone"]
    end = datetime.now(ZoneInfo(zone)).date() - timedelta(days=1)
    # A 59-day window stays safely within Shopify's default 60-day order access.
    first = end - timedelta(days=min(days, 59) - 1)
    begin_at = datetime.combine(first, datetime.min.time(), ZoneInfo(zone))
    end_at = datetime.combine(end + timedelta(days=1), datetime.min.time(), ZoneInfo(zone))
    query = f"created_at:>='{begin_at.isoformat()}' created_at:<'{end_at.isoformat()}'"
    cursor, seen = None, set()
    orders = {}
    while True:
        data = shopify_query(client, shop, token, ORDERS_QUERY, {"after": cursor, "query": query, "details": detailed})["orders"]
        for raw in data["nodes"]:
            normalized = normalize_order(raw)
            created = datetime.fromisoformat(normalized["created_at"].replace("Z", "+00:00"))
            if begin_at <= created < end_at:
                orders[normalized["order_id"]] = normalized
        info = data["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
        if not cursor or cursor in seen:
            raise ContentError("Shopify returned an invalid order pagination cursor.")
        seen.add(cursor)
    with database() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))", (f"commerce-orders:{org}",))
        cur.execute("""delete from marketing_shopify_orders where organization_id=%s and shop=%s
                       and created_at >= %s and created_at < %s""", (org, shop, begin_at, end_at))
        if orders:
            execute_values(cur, """insert into marketing_shopify_orders
                (organization_id,shop,order_id,created_at,updated_at,eligible,financial_status,amount,currency,
                 presentment,channel,attributed,utm) values %s
                on conflict (organization_id,shop,order_id) do update set
                created_at=excluded.created_at,updated_at=excluded.updated_at,eligible=excluded.eligible,
                financial_status=excluded.financial_status,amount=excluded.amount,currency=excluded.currency,
                presentment=excluded.presentment,channel=excluded.channel,attributed=excluded.attributed,utm=excluded.utm
            """, [(org, shop, *[row[key] for key in ("order_id","created_at","updated_at","eligible","financial_status",
                                                       "amount","currency")], Json(row["presentment"]), row["channel"],
                    row["attributed"], Json(row["utm"])) for row in orders.values()])
            if detailed:
                execute_values(cur, """update marketing_shopify_orders o set evaluation=v.evaluation::jsonb
                    from (values %s) as v(org,shop,order_id,evaluation)
                    where o.order_id=v.order_id and o.organization_id=v.org::uuid and o.shop=v.shop""",
                    [(org, shop, row['order_id'], Json(row['evaluation'])) for row in orders.values()])
        cur.execute("""insert into marketing_commerce_coverage
            (organization_id,source,account,start_day,end_day,timezone,currency,collected_at)
            values (%s,'shopify_orders',%s,%s,%s,%s,%s,now())
            on conflict (organization_id,source,account) do update set
            start_day=excluded.start_day,end_day=excluded.end_day,timezone=excluded.timezone,
            currency=excluded.currency,collected_at=excluded.collected_at
        """, (org, shop, first, end, zone, context["shop"]["currencyCode"]))
    return len(orders)


def import_sessions(client, org, property_id, zone, days, detailed=False):
    end = datetime.now(ZoneInfo(zone)).date() - timedelta(days=1)
    first = end - timedelta(days=days - 1)
    endpoint = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    headers = google_headers(endpoint, "analytics.readonly")
    request = {"dateRanges": [{"startDate": str(first), "endDate": str(end)}],
               "dimensions": [{"name": name} for name in ("date", "sessionSource", "sessionMedium", "sessionCampaignName")],
               "metrics": [{"name": name} for name in ("sessions", "itemViewEvents", "addToCarts", "checkouts",
                                                      "ecommercePurchases", "purchaseRevenue")],
               "limit": "10000", "offset": "0"}
    rows = []
    landing_rows = []
    seen_dimensions = set()
    if detailed:
        # A landing page is session-scoped. Do not add event-scoped hostName:
        # a cross-domain session could then be counted once per host when rolled up.
        request['dimensions'].append({'name': 'landingPagePlusQueryString'})
    request['orderBys'] = [{'dimension': {'dimensionName': d['name']}} for d in request['dimensions']]
    while True:
        body = analytics_response(client.post(endpoint, headers=headers, json=request))
        metadata = body.get("metadata", {})
        if (metadata.get("subjectToThresholding") or metadata.get("dataLossFromOtherRow") or metadata.get('samplingMetadatas')
                or metadata.get("schemaRestrictionResponse", {}).get("activeMetricRestrictions")):
            raise ContentError("GA4 restricted or thresholded the storewide report. Previous import was kept.")
        if metadata["timeZone"] != zone:
            raise ContentError("GA4 property time zone differs from CONTENT_GA4_TIMEZONE. Correct it and retry.")
        currency = metadata.get("currencyCode")
        page = body.get("rows", [])
        for row in page:
            dimensions = [entry["value"] for entry in row["dimensionValues"]]
            if tuple(dimensions) in seen_dimensions:
                raise ContentError('GA4 returned duplicate dimension rows. Previous import was kept.')
            seen_dimensions.add(tuple(dimensions))
            day, source, medium, campaign = dimensions[:4]
            metrics = {spec["name"]: float(value["value"]) for spec, value in zip(request["metrics"], row["metricValues"], strict=True)}
            parsed_day = datetime.strptime(day, '%Y%m%d').date()
            if not first <= parsed_day <= end or not all(math.isfinite(v) for v in metrics.values()):
                raise ContentError('GA4 returned invalid dates or metrics. Previous import was kept.')
            rows.append((org, property_id, datetime.strptime(day, "%Y%m%d").date(), source, medium, campaign, Json(metrics)))
            if detailed:
                landing_rows.append(dict(day=day, source=source, medium=medium, campaign=campaign,
                                         landing_page=dimensions[4], metrics=metrics))
        offset = int(request["offset"]) + len(page)
        if not page and offset < body.get("rowCount", offset):
            raise ContentError("GA4 returned an incomplete page. Previous import was kept.")
        if offset >= body.get("rowCount", offset):
            break
        request["offset"] = str(offset)
    with database() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))", (f"commerce-ga4:{org}",))
        cur.execute("""delete from marketing_ga4_store_daily where organization_id=%s and property_id=%s
                       and day between %s and %s""", (org, property_id, first, end))
        if rows:
            if detailed:
                # The richer request replaces, rather than duplicates, the existing storewide request.
                grouped = {}
                for row in rows:
                    key = row[:6]
                    total = grouped.setdefault(key, {name: 0 for name in row[6].adapted})
                    for name, value in row[6].adapted.items():
                        total[name] += value
                rows = [(*key, Json(value)) for key, value in grouped.items()]
            execute_values(cur, "insert into marketing_ga4_store_daily values %s", rows)
        if detailed:
            from backend.evaluation_data import save_snapshot
            save_snapshot(cur, org, 'ga4_landing', property_id,
                          {'rows': landing_rows, 'timezone': zone, 'currency': currency}, first, end)
        cur.execute("""insert into marketing_commerce_coverage
            (organization_id,source,account,start_day,end_day,timezone,currency,collected_at)
            values (%s,'ga4_store',%s,%s,%s,%s,%s,now())
            on conflict (organization_id,source,account) do update set
            start_day=excluded.start_day,end_day=excluded.end_day,timezone=excluded.timezone,
            currency=excluded.currency,collected_at=excluded.collected_at
        """, (org, property_id, first, end, zone, currency))
    return len(rows)


def summary(org, start, end):
    result = {"policy": POLICY, "attribution_model": ATTRIBUTION, "orders": [], "ga4": [], "coverage": [], "latest_runs": []}
    with database() as cur:
        cur.execute("select source,external_id,data,published_at from marketing_content_items where organization_id=%s and source='facebook'", (org,))
        owned = owned_tags(cur.fetchall())
        cur.execute("select * from marketing_commerce_coverage where organization_id=%s order by source,account", (org,))
        result["coverage"] = cur.fetchall()
        cur.execute("""select distinct on (source) source,status,records,message,finished_at
                       from marketing_content_sync_runs where organization_id=%s
                       and source in ('shopify_orders','ga4_store','ga4_channels') order by source,finished_at desc""", (org,))
        result["latest_runs"] = cur.fetchall()
        for row in result["coverage"]:
            row["complete"] = row["start_day"] <= start <= end <= row["end_day"]
            row["stale"] = datetime.now(timezone.utc) - row["collected_at"] > timedelta(hours=48)
            if not row["complete"]:
                continue
            if row["source"] == "shopify_orders":
                cur.execute("""select channel as stored_channel,utm,currency,min(created_at) as first_order_at,count(*) as purchases,sum(amount) as revenue,
                               count(*) filter (where attributed) as attributed_purchases,
                               sum(case when attributed then amount else 0 end) as attributed_revenue
                               from marketing_shopify_orders
                               where organization_id=%s and shop=%s and eligible
                                 and (created_at at time zone %s)::date between %s and %s
                               group by channel,utm,currency order by channel,currency""",
                            (org, row["account"], row["timezone"], start, end))
                rows = cur.fetchall()
                result["orders"].extend({**entry, **classify(entry["utm"], owned, occurred_at=entry["first_order_at"]), "shop": row["account"],
                                         "measurement_source": "Shopify canonical orders",
                                         "timezone": row["timezone"], "start": start, "end": end} for entry in rows)
                if not rows:
                    result["orders"].append({"shop": row["account"], "currency": row["currency"], "channel": "No eligible orders",
                                              "purchases": 0, "revenue": 0, "attributed_purchases": 0, "attributed_revenue": 0})
            if row["source"] == "ga4_store":
                cur.execute("""select source,medium,campaign,sum((metrics->>'sessions')::numeric) as sessions,
                              sum((metrics->>'itemViewEvents')::numeric) as product_views,
                              sum((metrics->>'addToCarts')::numeric) as add_to_carts,
                              sum((metrics->>'checkouts')::numeric) as checkouts,
                              sum((metrics->>'ecommercePurchases')::numeric) as ga4_purchases,
                              sum((metrics->>'purchaseRevenue')::numeric) as ga4_purchase_revenue
                              from marketing_ga4_store_daily where organization_id=%s and property_id=%s
                              and day between %s and %s group by source,medium,campaign order by source,medium,campaign""",
                            (org, row["account"], start, end))
                result["ga4"].extend({**entry, **classify({k: entry[k] for k in ("source", "medium", "campaign")}, owned), "property_id": row["account"], "currency": row["currency"],
                                     "measurement_source": "GA4 session attribution", "timezone": row["timezone"], "start": start, "end": end}
                                     for entry in cur.fetchall())
            if row["source"] == "ga4_channels":
                from backend.channel_data import report_rows
                cur.execute("select day,dimensions,metrics from marketing_channel_daily where organization_id=%s and property_id=%s and day between %s and %s order by day,segment",
                            (org, row["account"], start, end))
                result.setdefault("channel_ga4", []).extend({**entry, "property_id": row["account"], "timezone": row["timezone"],
                                                            "currency": row["currency"], "start": start, "end": end} for entry in report_rows(cur.fetchall(), owned, row["timezone"]))
    result["channel_coverage"] = channel_coverage(result)
    result["channel_comparison"] = comparisons(result)
    result["comparison_note"] = ("Same requested dates; Shopify order-creation dates and GA4 session attribution use their reported time zones. "
                                 "Do not add or force-match purchases or revenue across sources. Shopify has no imported traffic sessions; "
                                 "unready journeys and missing UTMs remain unattributed. The separate GA4 channel evidence table retains manual UTMs; "
                                 "it is another view of sessions, not additional sessions to add to storewide totals.")
    return result
