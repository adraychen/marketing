"""Reddit Ads v3 read-only discovery and daily reporting (POST reports is read-only)."""
import math
import re
import time
from datetime import date, timedelta
from urllib.parse import quote, urlsplit

import httpx

BASE = "https://ads-api.reddit.com/api/v3"
EVENTS = ("page_visit", "view_content", "add_to_cart", "sign_up", "lead", "purchase")
VIDEO = ("video_started", "video_viewable_impressions", "video_watched_25_percent",
         "video_watched_50_percent", "video_watched_75_percent", "video_watched_100_percent",
         "video_watched_3_seconds", "video_watched_10_seconds")
FIELDS = ["SPEND", "IMPRESSIONS", "CLICKS", "CURRENCY", "REACH", "FREQUENCY",
          *[f"CONVERSION_{event.upper()}_{kind}" for event in EVENTS for kind in ("CLICKS", "VIEWS")],
          "CONVERSION_PURCHASE_TOTAL_VALUE", *[key.upper() for key in VIDEO], "VIDEO_COMPLETION_RATE"]
BREAKDOWNS = {"PLACEMENT", "OS_TYPE", "COUNTRY", "COMMUNITY", "INTEREST", "KEYWORD", "ASSET_ID"}


class RedditError(Exception):
    """Only sanitized messages may be placed in this exception."""


def identifier(value):
    if not isinstance(value, str) or not value:
        raise RedditError("Reddit omitted a required resource ID.")
    return quote(value, safe="")


class Client:
    def __init__(self, http, token_provider, user_agent, sleep=time.sleep):
        self.http, self.token_provider, self.user_agent, self.sleep = http, token_provider, user_agent, sleep
        self.last_report = 0

    def request(self, method, path, body=None):
        url = path if path.startswith("https://") else BASE + path
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "ads-api.reddit.com" or not parts.path.startswith("/api/v3/"):
            raise RedditError("Reddit returned an unsafe pagination URL.")
        if method not in {"GET", "POST"} or method == "POST" and not parts.path.endswith("/reports"):
            raise RedditError("Only read-only Reddit requests are allowed.")
        refresh = False
        for attempt in range(4):
            if method == "POST":
                self.sleep(max(0, 1.05 - (time.monotonic() - self.last_report)))
                self.last_report = time.monotonic()
            token = self.token_provider(refresh)
            refresh = False
            try:
                response = self.http.request(method, url, json=body,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": self.user_agent}, follow_redirects=False)
            except httpx.HTTPError:
                if attempt == 3:
                    raise RedditError("Reddit network request failed; previous data retained.") from None
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 401 and attempt == 0:
                refresh = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    try:
                        resets = re.findall(r"\bt=(\d+)", response.headers.get("ratelimit", ""))
                        delay = float(response.headers.get("retry-after", max(map(int, resets)) + 1 if resets else 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    if delay > 60:
                        raise RedditError("Reddit rate limit requires a later retry; previous data retained.")
                    self.sleep(max(1, delay))
                    continue
            if not response.is_success:
                raise RedditError(f"Reddit read request failed (HTTP {response.status_code}); check adsread authorization and API access.")
            try:
                result = response.json()
            except ValueError:
                raise RedditError("Reddit returned invalid JSON.") from None
            if not isinstance(result, dict) or "data" not in result:
                raise RedditError("Reddit returned an incomplete response.")
            return result
        raise RedditError("Reddit request retries exhausted.")

    def pages(self, path, body=None):
        seen, result = set(), []
        while path:
            if path in seen or len(seen) >= 10000:
                raise RedditError("Reddit pagination repeated or exceeded its safety limit.")
            seen.add(path)
            data = self.request("POST" if body else "GET", path, body)
            rows = data["data"].get("metrics") if body and isinstance(data["data"], dict) else data["data"]
            if not isinstance(rows, list) or not isinstance(data.get("pagination"), dict):
                raise RedditError("Reddit returned incomplete pagination.")
            result.extend(rows)
            path = data["pagination"].get("next_url")
        return result

    def accounts(self):
        accounts = {}
        for business in self.pages("/me/businesses"):
            for account in self.pages(f"/businesses/{identifier(business['id'])}/ad_accounts"):
                accounts[account["id"]] = account
        return list(accounts.values())


def numeric(value, divisor=1):
    if value is None:
        return None
    try:
        number = float(value) / divisor
    except (TypeError, ValueError, OverflowError):
        raise RedditError("Reddit returned a nonnumeric metric.") from None
    if isinstance(value, bool) or not math.isfinite(number) or number < 0:
        raise RedditError("Reddit returned an invalid metric.")
    return number


def normalize(raw, account, known_events):
    currency = account.get("currency")
    if not currency or raw.get("currency") not in (None, currency):
        raise RedditError("Reddit reporting currency is missing or conflicts with the account.")
    metrics = {key: numeric(raw.get(key)) for key in ("impressions", "clicks", "reach", "frequency", *VIDEO, "video_completion_rate")}
    metrics["spend"] = numeric(raw.get("spend"), 1_000_000)
    availability = {}
    for event in EVENTS:
        for suffix in ("clicks", "views"):
            key = f"conversion_{event}_{suffix}"
            value = numeric(raw.get(key))
            # A zero without evidence of tracking must not imply a configured event.
            metrics[key] = value if value or event in known_events else None
            if metrics[key] is None:
                availability[key] = "Metric absent or event tracking not verified"
    value = numeric(raw.get("conversion_purchase_total_value"), 100)
    metrics["purchase_value"] = value if value or "purchase" in known_events else None
    attribution = account.get("attribution_type")
    click, view = (metrics[f"conversion_purchase_{key}"] for key in ("clicks", "views"))
    metrics["purchases"] = click if attribution == "CLICK_THROUGH_CONVERSION" else view if attribution == "VIEW_THROUGH_CONVERSION" else (
        click + view if attribution == "ALL_CONVERSION" and click is not None and view is not None else None)
    return metrics, availability


def fetch_account(client, account, start, end, extra_breakdowns=()):
    extra_breakdowns = tuple(dict.fromkeys(extra_breakdowns))
    if any(value not in BREAKDOWNS for value in extra_breakdowns):
        raise RedditError("Unsupported Reddit breakdown configuration.")
    root = f"/ad_accounts/{identifier(account['id'])}"
    metadata = {kind: client.pages(f"{root}/{kind}") for kind in ("campaigns", "ad_groups", "ads")}
    if any(ad.get("asset_identifiers") or ad.get("type") == "DCA_TEMPLATE" for ad in metadata["ads"]):
        extra_breakdowns = tuple(dict.fromkeys((*extra_breakdowns, "ASSET_ID")))
    for kind in ("campaigns", "ad_groups"):
        for item in metadata[kind]:
            item["budget_currency"] = account.get("currency")
            item["budget_amount"] = numeric(item.get("goal_value"), 1_000_000) if item.get("goal_type") in ("DAILY_SPEND", "LIFETIME_SPEND") else None
            item["bid_amount"] = numeric(item.get("bid_value"), 1_000_000)
    groups = {row["id"]: row for row in metadata["ad_groups"]}
    ads = {row["id"]: row for row in metadata["ads"]}
    for ad in metadata["ads"]:
        group_id = ad.get("ad_group_id")
        if group_id not in groups:
            group = client.request("GET", f"/ad_groups/{identifier(group_id)}")["data"]
            groups[group_id] = group
            metadata["ad_groups"].append(group)
    campaigns = {row["id"] for row in metadata["campaigns"]}
    for group in metadata["ad_groups"]:
        campaign_id = group.get("campaign_id")
        if campaign_id not in campaigns:
            metadata["campaigns"].append(client.request("GET", f"/campaigns/{identifier(campaign_id)}")["data"])
            campaigns.add(campaign_id)
    known, pixel_events, limitations = set(), {}, []
    try:
        for pixel in client.pages(root + "/pixels"):
            fired = client.request("GET", f"/pixels/{identifier(pixel['id'])}/last_fired_at")["data"]
            pixel_events[pixel["id"]] = {key for key in EVENTS if fired.get(key)}
            known.update(pixel_events[pixel["id"]])
    except RedditError as exc:
        limitations.append("Conversion tracking verification unavailable: " + str(exc))
    # Creative metadata is separate from metrics; optional read permissions must not lose delivery data.
    for ad in metadata["ads"]:
        if ad.get("post_id"):
            try:
                ad["post"] = client.request("GET", f"/structured_posts/{identifier(ad['post_id'])}")["data"]
            except RedditError:
                try:
                    ad["post"] = client.request("GET", f"/posts/{identifier(ad['post_id'])}")["data"]
                except RedditError:
                    limitations.append(f"Creative metadata unavailable for ad {ad['id']}.")
    observations = []
    for breakdown in ("base", *extra_breakdowns):
        if breakdown != "base" and breakdown not in BREAKDOWNS:
            raise RedditError("Unsupported Reddit breakdown configuration.")
        first = start
        pending, seen = [], set()
        try:
            while first <= end:
                last = min(first + timedelta(days=27), end)
                body = {"data": {"starts_at": f"{first}T00:00:00Z", "ends_at": f"{last + timedelta(days=1)}T00:00:00Z",
                    "time_zone_id": "UTC", "breakdowns": ["DATE", "AD_ID"] + ([] if breakdown == "base" else [breakdown]),
                    "fields": FIELDS}}
                for raw in client.pages(root + "/reports", body):
                    day = date.fromisoformat(raw["date"])
                    # Some boundary rows may be returned at the exclusive end. Do not store them twice.
                    if day == last + timedelta(days=1):
                        continue
                    if not first <= day <= last:
                        raise RedditError("Reddit returned dates outside the requested reporting window.")
                    ad_id = raw.get("ad_id")
                    identifier(ad_id)
                    if ad_id not in ads:
                        ad = client.request("GET", f"/ads/{identifier(ad_id)}")["data"]
                        ads[ad_id] = ad
                        metadata["ads"].append(ad)
                    ad = ads[ad_id]
                    group_id = ad.get("ad_group_id")
                    if group_id not in groups:
                        group = client.request("GET", f"/ad_groups/{identifier(group_id)}")["data"]
                        groups[group_id] = group
                        metadata["ad_groups"].append(group)
                    campaign_id = groups[group_id]["campaign_id"]
                    if not any(c["id"] == campaign_id for c in metadata["campaigns"]):
                        metadata["campaigns"].append(client.request("GET", f"/campaigns/{identifier(campaign_id)}")["data"])
                    segment = str(raw.get(breakdown.lower(), "")) if breakdown != "base" else ""
                    if breakdown != "base" and not segment:
                        raise RedditError("Reddit omitted a requested breakdown dimension.")
                    key = (str(day), ad_id, segment)
                    if key in seen:
                        raise RedditError("Reddit returned duplicate daily ad observations.")
                    seen.add(key)
                    tracking = pixel_events.get(groups[group_id].get("conversion_pixel_id"), set())
                    metrics, availability = normalize(raw, account, tracking)
                    pending.append({"day": str(day), "ad_id": ad_id, "ad_group_id": group_id, "campaign_id": campaign_id,
                        "breakdown": breakdown, "segment": segment, "currency": account["currency"], "metrics": metrics,
                        "availability": availability, "raw": raw})
                first = last + timedelta(days=1)
        except RedditError:
            if breakdown == "base":
                raise
            limitations.append(f"{breakdown} breakdown unavailable; no partial breakdown stored.")
            continue
        observations.extend(pending)
    return {"account": account, **metadata, "observations": observations, "limitations": limitations,
            "known_events": sorted(known), "start": str(start), "end": str(end),
            "attribution": {key: account.get(key) for key in ("attribution_type", "click_attribution_window", "view_attribution_window")},
            "timezone": "UTC", "source": "Reddit Ads API v3"}
