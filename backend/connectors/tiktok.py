import math
import os
from datetime import timedelta

import httpx

REPORT_URL = "https://business-api.tiktok.com/open_api/v2.0/report/integrated/get/"
DELIVERY_METRICS = ("spend", "impressions", "clicks", "conversion")


class TikTokReportError(Exception):
    pass


def advertiser_for(org):
    if org != os.getenv("TIKTOK_ADS_ORGANIZATION_ID", "").strip():
        return ""
    advertiser = os.getenv("TIKTOK_ADS_ADVERTISER_ID", "").strip()
    return advertiser if advertiser.isascii() and advertiser.isdecimal() else ""


def configured(org):
    return bool(advertiser_for(org) and os.getenv("TIKTOK_ADS_ACCESS_TOKEN", "").strip())


def numeric(value):
    if value in (None, "", "-"):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def campaign_report(client, advertiser, token, start, end):
    rows = []
    first = start
    while first <= end:
        last = min(first + timedelta(days=29), end)
        query = {
            "advertiser_ids": [advertiser],
            "dimensions": ["campaign_id", "stat_time_day"],
            "metrics": ["campaign_name", "currency", "timezone", *DELIVERY_METRICS],
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
            "page": 1,
            "page_size": 1000,
        }
        while True:
            response = client.post(REPORT_URL, headers={"Access-Token": token}, json=query)
            if not response.is_success:
                raise TikTokReportError(f"TikTok returned HTTP {response.status_code}. Check advertiser access and reporting permissions.")
            payload = response.json()
            if payload.get("code") != 0:
                code = payload.get("code")
                suffix = f" (code {code})" if isinstance(code, int) else ""
                raise TikTokReportError(f"TikTok rejected the report request{suffix}. Check advertiser access and reporting permissions.")
            data = payload["data"]
            for entry in data["list"]:
                dims, metrics = entry["dimensions"], entry["metrics"]
                rows.append({
                    "campaign_id": dims["campaign_id"],
                    "campaign_name": metrics.get("campaign_name") or dims["campaign_id"],
                    "day": dims["stat_time_day"],
                    "currency": metrics.get("currency"),
                    "timezone": metrics.get("timezone"),
                    **{metric: numeric(metrics.get(metric)) for metric in DELIVERY_METRICS},
                })
            pages = int(data["page_info"]["total_page"])
            if query["page"] >= pages:
                break
            if query["page"] >= 20:
                raise TikTokReportError("TikTok returned too many rows. Choose a shorter date range.")
            query["page"] += 1
        first = last + timedelta(days=1)
    return rows
