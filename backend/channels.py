"""Versioned reporting labels; raw attribution is never rewritten."""
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timezone

VERSION = "2026-11-17.1"
CHANNELS = ("Google Paid Search", "Google Organic Search", "Meta Paid — Facebook", "Meta Paid — Instagram",
            "Facebook Organic Content", "Facebook Referral — Unattributed", "Instagram Organic Content",
            "Instagram Referral — Unattributed", "Shopify Blog / Organic Search", "Other Referral", "Direct / Unknown",
            "Meta Paid — Platform Unknown", "ChatGPT Paid", "ChatGPT Referral — Unattributed")
FIELDS = ("source", "medium", "campaign", "content")


def owned_tags(items):
    """Only a unique, imported content item establishes an owned-content match."""
    tags = {}
    for item in items:
        for url in item.get("data", {}).get("links", []):
            try:
                query = parse_qs(urlsplit(url).query)
            except ValueError:
                continue
            if any(len(query.get("utm_" + key, [])) != 1 for key in FIELDS):
                continue
            key = tuple(query["utm_" + name][0] for name in FIELDS)
            if all(key):
                tags.setdefault(key, set()).add((item["source"], str(item["external_id"]), str(item.get("published_at") or "")))
    return {key: next(iter(ids)) for key, ids in tags.items() if len(ids) == 1}


def classify(raw, owned=None, landing_path=None, occurred_at=None):
    source = (raw.get("source") or "").strip().lower()
    medium = (raw.get("medium") or "").strip().lower()
    # Exact host matching prevents domains such as notinstagram.com from matching.
    try:
        host = urlsplit(source if "://" in source else "//" + source).hostname or source
    except ValueError:
        host = source
    facebook = source in {"facebook", "fb"} or host in {"facebook.com", "www.facebook.com", "m.facebook.com", "l.facebook.com", "lm.facebook.com"}
    instagram = source in {"instagram", "ig"} or host in {"instagram.com", "www.instagram.com", "l.instagram.com", "m.instagram.com"}
    paid = medium in {"cpc", "ppc", "paid_social", "paid_search", "paid"}
    match = (owned or {}).get(tuple(raw.get(key) or "" for key in FIELDS))
    if match:
        try:
            publication = datetime.fromisoformat(match[2].replace("Z", "+00:00"))
            occurred = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
            if not publication.tzinfo:
                publication = publication.replace(tzinfo=timezone.utc)
            if not occurred.tzinfo:
                occurred = occurred.replace(tzinfo=timezone.utc)
            if publication > occurred:
                match = None
        except (ValueError, IndexError, TypeError):
            match = None
    content_id = None
    reason = "No reliable source/medium channel evidence"
    channel = "Direct / Unknown"
    if source in {"chatgpt", "openai"} or host in {"chatgpt.com", "www.chatgpt.com"}:
        channel = "ChatGPT Paid" if paid else "ChatGPT Referral — Unattributed"
        reason = "Explicit paid medium" if paid else "ChatGPT origin alone does not establish paid-ad attribution"
    elif source in {"google", "google_ads"} and medium in {"cpc", "ppc", "paid_search"}:
        channel, reason = "Google Paid Search", "Explicit Google paid-search source/medium; not an Ads campaign-type assertion"
    elif source == "google" and medium == "organic":
        channel = "Shopify Blog / Organic Search" if (landing_path or "").startswith("/blogs/") else "Google Organic Search"
        reason = "Google organic source/medium" + (" and known blog landing path" if landing_path else "")
    elif (facebook or instagram) and paid:
        channel, reason = "Meta Paid — " + ("Facebook" if facebook else "Instagram"), "Explicit platform source and paid medium; not verified ad delivery"
    elif source == "meta" and paid:
        channel, reason = "Meta Paid — Platform Unknown", "Paid Meta tag does not identify publisher platform"
    elif facebook or instagram:
        platform = "Facebook" if facebook else "Instagram"
        if medium == "organic_social" and match and match[0] == platform.lower():
            channel, reason, content_id = platform + " Organic Content", "Exact unique UTM match to imported owned content", match[1]
        else:
            channel, reason = platform + " Referral — Unattributed", "Platform origin only; no unique owned-content match"
    elif source == "shopify_blog" and medium == "content":
        channel, reason = "Shopify Blog / Organic Search", "Explicit Shopify blog source/medium tag; no article attribution"
    elif medium == "referral" and source not in {"", "(direct)", "(not set)"}:
        channel, reason = "Other Referral", "Referral source without owned-campaign evidence"
    return {"channel": channel, "classification_version": VERSION, "classification_reason": reason,
            "content_id": content_id, "raw_attribution": dict(raw),
            "campaign_attribution": "Matched owned content" if content_id else "Unattributed to a specific campaign/content item"}


def coverage(data):
    """Coverage is per source/channel, not evidence that a channel had activity."""
    rows = []
    for channel in CHANNELS:
        for source, label, metrics in (
            ("ga4_store", "GA4", ("sessions", "product_views", "add_to_carts", "checkouts", "ga4_purchases", "ga4_purchase_revenue")),
            ("ga4_channels", "GA4 manual UTM evidence", ("sessions", "product_views", "add_to_carts", "checkouts", "ga4_purchases", "ga4_purchase_revenue")),
            ("shopify_orders", "Shopify canonical orders", ("purchases", "revenue")),
        ):
            imported = [r for r in data.get("coverage", []) if r["source"] == source]
            runs = [r for r in data.get("latest_runs", []) if r["source"] == source]
            records = [r for r in data.get({"ga4_store": "ga4", "ga4_channels": "channel_ga4", "shopify_orders": "orders"}[source], []) if r.get("channel") == channel]
            available = [key for key in metrics if any(r.get(key) is not None for r in records)]
            state = "available" if available else "unavailable" if imported else "disconnected" if not runs or runs[0]["status"] == "skipped" else "unavailable"
            if channel in {"Instagram Organic Content", "Facebook Organic Content"} and not records:
                state = "untracked"
            rows.append({"channel": channel, "data_source": label, "state": state,
                         "last_successful_sync": max((r["collected_at"] for r in imported), default=None),
                         "available_metrics": available, "unavailable_metrics": [m for m in metrics if m not in available],
                         "metric_states": {m: "unavailable" if m not in available else "zero" if all(r.get(m) == 0 for r in records) else "available" for m in metrics},
                         "attribution_limitations": "Sources use different attribution and counting rules. Missing channel rows are not proof of zero. Organic content requires a unique imported-content UTM match.",
                         "tracking_or_permission_errors": runs[0].get("message") if runs else None})
    return rows


def comparisons(data):
    """Side-by-side measurements, never a joined or deduplicated purchase total."""
    rows = []
    for provider, key, account_key, purchase_key, revenue_key in (
        ("Shopify canonical", "orders", "shop", "purchases", "revenue"),
        ("GA4 session attribution", "ga4", "property_id", "ga4_purchases", "ga4_purchase_revenue"),
        ("GA4 manual UTM evidence (alternative breakdown)", "channel_ga4", "property_id", "ga4_purchases", "ga4_purchase_revenue"),
    ):
        grouped = {}
        for row in data.get(key, []):
            group = tuple(row.get(k) for k in (account_key, "channel", "currency", "timezone", "start", "end"))
            if group not in grouped:
                grouped[group] = {"provider": provider, "account": group[0], "channel": group[1], "currency": group[2],
                                  "timezone": group[3], "start": group[4], "end": group[5],
                                  "purchases": None, "revenue": None, "sessions": None}
            target = grouped[group]
            for output, field in (("purchases", purchase_key), ("revenue", revenue_key), ("sessions", "sessions")):
                if row.get(field) is not None:
                    target[output] = (target[output] or 0) + row[field]
        rows.extend(grouped.values())
    return rows
