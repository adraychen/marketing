"""Read-only Meta Marketing API client. Never follows token-bearing paging URLs."""
import json
import math
import os
import re
from datetime import date, datetime, timezone


class MetaAdsError(Exception):
    pass


def configuration(org):
    if str(org) != os.getenv("META_ADS_ORGANIZATION_ID", "").strip():
        return None
    account = os.getenv("META_ADS_ACCOUNT_ID", "").strip().removeprefix("act_")
    token = os.getenv("META_ADS_ACCESS_TOKEN", "").strip()
    version = os.getenv("META_ADS_API_VERSION", "v26.0").strip()
    if not account or not token:
        return None
    if not re.fullmatch(r"[0-9]+", account) or not re.fullmatch(r"v[0-9]+[.][0-9]+", version):
        raise MetaAdsError("Check META_ADS_ACCOUNT_ID and META_ADS_API_VERSION.")
    expiry = os.getenv("META_ADS_TOKEN_EXPIRES_AT", "").strip()
    if expiry:
        try:
            parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            expiry = parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            raise MetaAdsError("META_ADS_TOKEN_EXPIRES_AT must be an ISO timestamp with timezone, or blank.") from None
    return {"account_id": account, "token": token, "version": version, "token_expires_at": expiry or None}


class Reader:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.base = f"https://graph.facebook.com/{config['version']}"

    def get(self, path, params=None):
        response = self.client.get(f"{self.base}/{path}", params=params or {},
                                   headers={"Authorization": f"Bearer {self.config['token']}"})
        try:
            body = response.json()
        except ValueError:
            raise MetaAdsError("Meta returned an unreadable response. Previous import was retained.") from None
        if not isinstance(body,dict):
            raise MetaAdsError("Meta returned an unexpected response. Previous import was retained.")
        error = body.get("error") or {}
        if not isinstance(error,dict):
            raise MetaAdsError("Meta returned an unexpected error. Previous import was retained.")
        if error or not response.is_success:
            code = error.get("code")
            if code == 190 or response.status_code == 401:
                raise MetaAdsError("Meta token expired or invalid. Renew the paid-ads token in Render.")
            if code in (10, 200, 294) or response.status_code == 403:
                raise MetaAdsError("Meta denied paid-ads access. Check ads_read and the system user's assigned ad account.")
            if code in (4, 17, 32, 613) or response.status_code == 429:
                raise MetaAdsError("Meta reporting rate limit reached. Retry later; previous import was retained.")
            raise MetaAdsError("Meta paid-ads request failed. Check API version, account access, and requested fields.")
        return body

    def rows(self, path, params):
        params = {**params, "limit": "100"}
        seen = set()
        while True:
            body = self.get(path, params)
            if not isinstance(body.get("data"), list):
                raise MetaAdsError("Meta returned incomplete paging data. Previous import was retained.")
            yield from body["data"]
            paging = body.get("paging") or {}
            if not isinstance(paging,dict):
                raise MetaAdsError("Meta returned incomplete pagination. Previous import was retained.")
            if not paging.get("next"):
                break
            cursors = paging.get("cursors") or {}
            if not isinstance(cursors,dict):
                raise MetaAdsError("Meta returned incomplete pagination. Previous import was retained.")
            cursor = cursors.get("after")
            if not cursor or cursor in seen or len(seen) >= 1000:
                raise MetaAdsError("Meta returned incomplete pagination. Choose a shorter import period.")
            seen.add(cursor)
            params["after"] = cursor


def number(value):
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise MetaAdsError("Meta returned an invalid metric. Previous import was retained.")
    return result


def action_value(rows, action):
    if rows is not None and (not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows)):
        raise MetaAdsError("Meta returned invalid conversion actions. Previous import was retained.")
    matches = [row for row in rows or [] if row.get("action_type") == action]
    if len(matches) > 1:
        raise MetaAdsError("Meta returned ambiguous conversion categories. Previous import was retained.")
    return number(matches[0]["value"]) if matches else None


def normalize_metric(raw):
    if not all(isinstance(raw.get(key),str) and raw[key] for key in ("ad_id","adset_id","campaign_id")):
        raise MetaAdsError("Meta returned missing ad identifiers. Previous import was retained.")
    return {**{key: raw.get(key) for key in ("ad_id", "ad_name", "adset_id", "adset_name", "campaign_id", "campaign_name")},
            "day": raw["date_start"], "attribution_setting": raw.get("attribution_setting"),
            **{key: number(raw.get(key)) for key in ("spend", "impressions", "clicks", "inline_link_clicks", "reach", "frequency")},
            "website_purchases": action_value(raw.get("actions"), "offsite_conversion.fb_pixel_purchase"),
            "website_purchase_value": action_value(raw.get("action_values"), "offsite_conversion.fb_pixel_purchase"),
            "actions": raw.get("actions", []), "action_values": raw.get("action_values", [])}


def fetch(client, config, start, end):
    reader = Reader(client, config)
    account_path = f"act_{config['account_id']}"
    account = reader.get(account_path, {"fields": "id,account_id,name,currency,timezone_name,account_status"})
    if str(account.get("account_id")) != config["account_id"]:
        raise MetaAdsError("Meta returned a different ad account. Check the paid-ads configuration.")
    fields = {
        "campaigns": "id,name,status,effective_status,objective,buying_type,daily_budget,lifetime_budget,bid_strategy,start_time,stop_time,created_time,updated_time",
        "adsets": "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,bid_strategy,bid_amount,billing_event,optimization_goal,start_time,end_time,attribution_spec,targeting,created_time,updated_time",
        "ads": "id,name,campaign_id,adset_id,status,effective_status,created_time,updated_time,creative{id,name,title,body,call_to_action_type,object_story_id,effective_object_story_id,object_story_spec,asset_feed_spec,url_tags,link_url,image_url,video_id,thumbnail_url}"
    }
    # These flags include paused/archived/deleted resources when accessible to the caller.
    base_statuses = ["ACTIVE", "PAUSED", "ARCHIVED", "DELETED", "IN_PROCESS", "WITH_ISSUES"]
    statuses = {"campaigns": base_statuses, "adsets": base_statuses + ["CAMPAIGN_PAUSED"],
                "ads": base_statuses + ["CAMPAIGN_PAUSED", "ADSET_PAUSED", "PENDING_REVIEW", "DISAPPROVED",
                                         "PREAPPROVED", "PENDING_BILLING_INFO"]}
    resources = {kind: list(reader.rows(f"{account_path}/{kind}", {"fields": names, "effective_status": json.dumps(statuses[kind])}))
                 for kind, names in fields.items()}
    for kind, rows in resources.items():
        if len({row["id"] for row in rows}) != len(rows):
            raise MetaAdsError("Meta returned duplicate metadata rows. Previous import was retained.")
    metrics = list(reader.rows(f"{account_path}/insights", {
        "level": "ad", "time_increment": "1", "time_range": json.dumps({"since":str(start), "until":str(end)}),
        "use_unified_attribution_setting": "true", "action_report_time": "impression",
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,date_start,date_stop,spend,impressions,clicks,inline_link_clicks,reach,frequency,actions,action_values,attribution_setting"
    }))
    normalized = {}
    for raw in metrics:
        date.fromisoformat(raw["date_start"])
        if raw["date_start"] != raw["date_stop"] or not str(start) <= raw["date_start"] <= str(end):
            raise MetaAdsError("Meta returned unexpected report dates. Previous import was retained.")
        row = normalize_metric(raw)
        key = (row["ad_id"], row["day"])
        if key in normalized:
            raise MetaAdsError("Meta returned duplicate daily ad rows. Previous import was retained.")
        normalized[key] = row
    return account, resources, list(normalized.values())
