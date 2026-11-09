from collections import defaultdict
from datetime import date

from backend.metrics import previous_period, summarize

LEVELS = ("campaigns", "keywords", "search_terms")
MEASURES = ("spend", "clicks", "conversions", "value", "cpc", "roas")
MIN_CLICKS = 20


def iso_date(value):
    return value.isoformat() if isinstance(value, date) else value


def weekly_review(report, account, start, end):
    prior_start, prior_end = previous_period(start, end)
    first, split, last = prior_start.isoformat(), start.isoformat(), end.isoformat()
    result = {
        "start": split, "end": last, "prior_start": first, "prior_end": prior_end.isoformat(),
        "coverage": bool(account.get("synced_from") and account.get("synced_through")
                         and iso_date(account["synced_from"]) <= first
                         and last <= iso_date(account["synced_through"])),
        "has_activity": False, "ai_ready": False, "drivers": {}, "actions": [],
    }
    if not result["coverage"]:
        return result

    currency = account["currency"]
    grouped = {level: defaultdict(lambda: ([], [])) for level in LEVELS}
    for row in report["performance"]:
        day = iso_date(row["day"])
        if row["kind"] in grouped and first <= day <= last:
            grouped[row["kind"]][row["resource"]][int(day < split)].append(row)

    campaign_rows = grouped["campaigns"].values()
    current = summarize((row for period in campaign_rows for row in period[0]), currency)
    previous = summarize((row for period in campaign_rows for row in period[1]), currency)
    result.update(current=current, previous=previous, has_activity=any(
        current[key] or previous[key] for key in ("spend", "impressions", "clicks", "conversions")
    ), ai_ready=min(current["clicks"], previous["clicks"]) >= MIN_CLICKS)
    if not result["has_activity"]:
        return result

    resources = {level: {item["resource"]: item for item in report[level]} for level in LEVELS}
    groups = {item["resource"]: item for item in report["ad_groups"]}
    candidates = []
    for level, records in grouped.items():
        drivers = []
        for resource, (current_rows, prior_rows) in records.items():
            now, before = summarize(current_rows, currency), summarize(prior_rows, currency)
            item = resources[level].get(resource, {})
            data = item.get("data", {})
            group = groups.get(item.get("parent"), {})
            campaign = resources["campaigns"].get(group.get("parent"), {}).get("data", {})
            driver = {
                "label": data.get("name") or data.get("text") or resource,
                "campaign": campaign.get("name"), "ad_group": group.get("data", {}).get("name"),
                "status": data.get("status"), "match_type": data.get("match_type"),
                "daily_budget": data.get("daily_budget"), "campaign_type": data.get("type"),
                "bidding_strategy": data.get("bidding_strategy"),
                "current": {key: now[key] for key in MEASURES},
                "previous": {key: before[key] for key in MEASURES},
                "spend_delta": now["spend"] - before["spend"],
                "conversion_delta": now["conversions"] - before["conversions"],
                "value_delta": now["value"] - before["value"]
                if now["value"] is not None and before["value"] is not None else None,
            }
            drivers.append(driver)
            enough_clicks = now["clicks"] >= MIN_CLICKS
            if (level == "search_terms" and enough_clicks and now["spend"] > 0
                    and now["conversions"] == 0 and before["conversions"] == 0
                    and (previous["cpa"] is None or now["spend"] >= previous["cpa"])):
                candidates.append((now["spend"], "search_term", driver))
            if (level == "campaigns" and enough_clicks and before["conversions"] >= 3
                    and driver["spend_delta"] > 0 and driver["conversion_delta"] < 0):
                candidates.append((driver["spend_delta"], "campaign", driver))
        result["drivers"][level] = drivers

    for _, kind, driver in sorted(candidates, key=lambda item: item[0], reverse=True)[:5]:
        result["actions"].append({"kind": kind, **driver})
    return result
