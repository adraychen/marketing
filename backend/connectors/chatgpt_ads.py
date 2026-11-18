"""Read-only OpenAI Advertiser API reporting; never logs credentials or upstream bodies."""
import json
import math
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BASE = "https://api.ads.openai.com/v1"
METRICS = ("spend", "impressions", "clicks", "ctr", "cpc", "cpm", "conversions", "cpa",
           "post_click_cvr", "order_created_attributed_sales", "order_created_roas")


class AdsError(Exception):
    pass


def configuration(org):
    if str(org) != os.getenv("OPENAI_ADS_ORGANIZATION_ID", "").strip():
        return None
    key = os.getenv("OPENAI_ADS_API_KEY", "").strip()
    if not key:
        return None
    try:
        click = int(os.getenv("OPENAI_ADS_CLICK_WINDOW_DAYS", "30"))
        view = int(os.getenv("OPENAI_ADS_VIEW_WINDOW_DAYS", "1"))
        if click not in (7, 14, 30) or view not in (0, 1):
            raise ValueError
    except ValueError:
        raise AdsError("Use a 7, 14 or 30 day click window and a 0 or 1 day view window.") from None
    return {"key": key, "account_id": os.getenv("OPENAI_ADS_ACCOUNT_ID", "").strip(), "click_window": click, "view_window": view}


def get(client, config, path, params=None):
    response = client.get(BASE + path, headers={"Authorization": "Bearer " + config["key"], "Accept": "application/json"}, params=params)
    if not response.is_success:
        messages = {401: "Advertiser API key is invalid or expired.", 403: "Advertiser API access denied for this account.",
                    429: "Advertiser reporting rate limit reached. Retry later."}
        raise AdsError(messages.get(response.status_code, f"Advertiser report request failed (HTTP {response.status_code}); prior data retained."))
    try:
        body = response.json()
    except ValueError:
        raise AdsError("Advertiser API returned an unreadable response.") from None
    if not isinstance(body, dict) or body.get("error"):
        raise AdsError("Advertiser API returned an invalid response.")
    return body


def numeric(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise AdsError("Advertiser API returned an invalid metric.")
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise AdsError("Advertiser API returned an invalid metric.") from None
    if not math.isfinite(number) or number < 0:
        raise AdsError("Advertiser API returned an invalid metric.")
    return number


def fetch(client, config, days):
    if not 1 <= days <= 365:
        raise AdsError("ChatGPT Ads lookback must be between 1 and 365 days.")
    account = get(client, config, "/ad_account")
    if not isinstance(account.get("id"), str) or not account["id"]:
        raise AdsError("Advertiser API omitted the account ID.")
    if config.get("account_id") and config["account_id"] != account["id"]:
        raise AdsError("Advertiser API key belongs to a different ad account.")
    zone = ZoneInfo(account["timezone"])
    if not account.get("currency_code"):
        raise AdsError("Advertiser API omitted the account currency.")
    end = datetime.now(zone).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    fields = ["metadata.readable_time", "campaign.id", "ad.id", "ad.name",
              *["ad." + key for key in ("spend", "impressions", "clicks", "ctr", "cpc", "cpm")],
              "conversions", "cpa", "post_click_cvr", "order_created_attributed_sales",
              "order_created_attributed_sales_currency", "order_created_roas"]
    params = [("aggregation_level", "ad"), ("time_granularity", "daily"), ("limit", "2000"),
              ("attribution_window_days", str(config["click_window"])),
              ("view_through_attribution_window_days", str(config["view_window"])),
              ("attribution_time_basis", "ad_event_time"),
              ("time_ranges[]", json.dumps({"type": "date_range", "since": str(start), "until": str(end), "timezone": account["timezone"]})),
              *[("fields[]", field) for field in fields]]
    rows, keys, cursors = [], set(), set()
    while True:
        body = get(client, config, "/ad_account/insights", params)
        if not isinstance(body.get("data"), list) or type(body.get("has_more")) is not bool:
            raise AdsError("Advertiser API returned incomplete pagination.")
        for raw in body["data"]:
            day = date.fromisoformat(raw["readable_time"])
            if not start <= day <= end or any(not isinstance(raw.get(k), str) or not raw[k] for k in ("ad_id", "campaign_id")):
                raise AdsError("Advertiser API returned unexpected dates or missing identifiers.")
            key = (raw["ad_id"], str(day))
            if key in keys:
                raise AdsError("Advertiser API returned duplicate daily ad rows.")
            keys.add(key)
            currency = raw.get("order_created_attributed_sales_currency")
            if currency and currency != account["currency_code"]:
                raise AdsError("Advertiser API returned mixed sales currencies.")
            rows.append({"day": str(day), "ad_id": raw["ad_id"], "campaign_id": raw["campaign_id"],
                         "ad_name": raw.get("ad_name"), **{key: numeric(raw.get(key)) for key in METRICS},
                         "currency": account["currency_code"], "raw": raw})
        if not body["has_more"]:
            break
        cursor = body.get("last_id")
        if not isinstance(cursor, str) or not cursor or cursor in cursors or not body["data"] or len(cursors) >= 1000:
            raise AdsError("Advertiser API returned incomplete or repeated pagination.")
        cursors.add(cursor)
        params = [(k, v) for k, v in params if k != "after"] + [("after", cursor)]
    return {"account": {k: account.get(k) for k in ("id", "name", "timezone", "currency_code", "status")},
            "start": str(start), "end": str(end), "daily": rows,
            "attribution": {"click_window_days": config["click_window"], "view_window_days": config["view_window"],
                            "time_basis": "ad_event_time", "source": "OpenAI Advertiser API", "grain": "daily ad"}}
