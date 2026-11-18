"""Publisher-platform observations kept separate from unbroken campaign totals."""
from datetime import date
from collections import defaultdict
from backend.connectors.meta_ads import Reader, MetaAdsError, insight_params, normalize_metric, action_value

METRICS = ("spend", "impressions", "inline_link_clicks", "landing_page_views", "website_purchases", "website_purchase_value")
NOTE = ("Meta daily ad/publisher-platform observations; account time zone, unified ad-set attribution, "
        "impression-time reporting. Reach is unique only within each returned row and is never summed. "
        "These rows are a breakdown of delivery, not additional purchases. Campaign totals continue to use "
        "unbroken ad rows. Shopify orders and GA4 purchases are separate measurements.")


def fetch_platforms(client, config, start, end):
    params = {**insight_params(start, end), "breakdowns": "publisher_platform"}
    rows, seen = [], set()
    for raw in Reader(client, config).rows(f"act_{config['account_id']}/insights", params):
        day = date.fromisoformat(raw["date_start"])
        if not start <= day <= end or raw["date_start"] != raw["date_stop"]:
            raise MetaAdsError("Meta platform report returned unexpected dates.")
        platform = raw.get("publisher_platform")
        if not isinstance(platform, str) or not platform:
            raise MetaAdsError("Meta platform report omitted publisher platform.")
        row = {**normalize_metric(raw), "publisher_platform": platform,
               "landing_page_views": action_value(raw.get("actions"), "landing_page_view")}
        key = (row["ad_id"], row["day"], platform)
        if key in seen:
            raise MetaAdsError("Meta platform report returned duplicate rows.")
        seen.add(key)
        rows.append(row)
    return rows


def reconcile(unbroken, platforms):
    """Flag disagreements; never substitute one grain for another."""
    grouped = defaultdict(list)
    for row in platforms:
        grouped[row["ad_id"], row["day"]].append(row)
    issues = []
    for row in unbroken:
        if not row.get("ad_id") or not row.get("day"):
            continue
        children = grouped.pop((row["ad_id"], row["day"]), [])
        if not children:
            issues.append({"ad_id": row["ad_id"], "day": row["day"], "issue": "No platform rows returned"})
            continue
        for metric in METRICS:
            values = [r.get(metric) for r in children]
            if row.get(metric) is None or any(v is None for v in values):
                continue
            total = sum(values)
            if abs(total - row[metric]) > 0.011:
                issues.append({"ad_id": row["ad_id"], "day": row["day"], "metric": metric,
                               "unbroken_value": row[metric], "platform_sum": total,
                               "issue": "Breakdown disagreement; review attribution settings, API revision and reporting completeness"})
        if any(c.get("attribution_setting") != row.get("attribution_setting") for c in children):
            issues.append({"ad_id": row["ad_id"], "day": row["day"], "issue": "Attribution settings differ"})
    for ad, day in grouped:
        issues.append({"ad_id": ad, "day": day, "issue": "Platform rows have no unbroken counterpart"})
    return issues
