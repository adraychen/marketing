"""Factual post reporting shared by the UI and exports."""
import json
from urllib.parse import parse_qs, urlsplit

from backend.metrics import ratio
from frontend.visibility import facebook_latest

LIMITATION = ("Destination-page context, not conversions attributed to this post. Facebook source/medium "
              "can include other posts and referrals. Daily users and sessions are not additive across "
              "days, traffic scopes or posts sharing a destination. Purchases and revenue belong to "
              "sessions landing on the page; page-view sessions do not establish subsequent purchases.")
TRACKED_URL = ("https://okoostore.com/pages/black-friday-watches?utm_source=facebook&"
               "utm_medium=organic_social&utm_campaign=black_friday_2026&utm_content=preview_post")


def post_report(details, kind):
    latest = facebook_latest(details.get("snapshots", []), kind)
    groups = {
        "Facebook visibility": ("reach", "impressions", "organic_distribution", "post_media_view",
                                "post_total_media_view_unique", "reactions", "comments", "shares"),
        "Facebook engagement": ("post_clicks", "link_clicks", "outbound_clicks"),
    }
    metrics = [{"group": group, "metric": field, "value": latest.get(field),
                "source": "Facebook Graph API", "period": "Lifetime at observation",
                "observed_at": latest.get("observed_at"),
                "availability": latest["availability"].get(field, "Unavailable in supported post import; no Page metric substituted")}
               for group, fields in groups.items() for field in fields]
    diagnostics = []
    for url in details.get("metadata", {}).get("links", []):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            continue
        query = parse_qs(parts.query)
        tagged = all(query.get(key, [""])[0] for key in
                     ("utm_source", "utm_medium", "utm_campaign", "utm_content"))
        diagnostics.append({"destination": parts._replace(query="", fragment="").geturl(),
                            "diagnostic": "Tag present; uniqueness and collected attribution must be verified." if tagged else
                            "Post-specific attribution unavailable: destination URL lacks unique UTM tagging.",
                            "suggested_future_url": TRACKED_URL if parts.hostname == "okoostore.com" and
                            parts.path.rstrip("/") == "/pages/black-friday-watches" else None})
    outcomes = []
    for row in details.get("daily", []):
        if row["report"] != "ga4_traffic":
            continue
        segment = json.loads(row["segment"])
        if "destination_path" not in segment:
            continue
        values = row["metrics"]
        outcomes.append({"source": "GA4", "day": row["day"], "property": row["source_account"],
                         "timezone": row["timezone"], "currency": row.get("currency"), **segment, **values,
                         "average_engagement_seconds_per_session": ratio(values.get("userEngagementDuration"), values.get("sessions"))})
    return metrics, diagnostics, outcomes
