from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.connectors.content import facebook_video_reference
from backend.metrics import ratio

VIDEO_FIELDS = ("total_video_views", "total_video_view_total_time", "total_video_avg_time_watched",
                "total_video_retention_graph", "total_video_60s_excludes_shorter_views")
POST_FIELDS = ("post_media_view", "post_total_media_view_unique", "post_clicks",
               "reactions", "comments", "shares", "link_clicks")
CUMULATIVE_FIELDS = ("post_media_view", "post_clicks", "total_video_views", "total_video_view_total_time")


def source_status(run):
    if not run:
        return "No sync recorded"
    if run["status"] == "succeeded":
        return "Succeeded with data" if run.get("records") else "Succeeded with no matching records"
    return run["status"]


def facebook_kind(item, metadata):
    permalink_type, _ = facebook_video_reference(item.get("url"))
    return permalink_type or metadata.get("content_type") or "unknown"


def search_totals(rows):
    if not rows:
        return {key: None for key in ("impressions", "clicks", "ctr", "position")}
    impressions = sum(row["metrics"]["impressions"] for row in rows)
    clicks = sum(row["metrics"]["clicks"] for row in rows)
    weighted = sum(row["metrics"]["position"] * row["metrics"]["impressions"] for row in rows)
    return {"impressions": impressions, "clicks": clicks, "ctr": ratio(clicks, impressions),
            "position": ratio(weighted, impressions)}


def search_queries(rows):
    queries = defaultdict(list)
    for row in rows:
        if row["report"] == "gsc_query":
            queries[row["source_account"], row["segment"]].append(row)
    return sorted(
        ({"property": source, "query": query, **search_totals(entries)} for (source, query), entries in queries.items()),
        key=lambda row: (-row["clicks"], -row["impressions"], row["query"]),
    )


def seo_review(item, metadata):
    title, description = metadata.get("seo_title"), metadata.get("meta_description")
    indexing = metadata.get("indexing") or {}
    canonical = metadata.get("canonical_url")
    image = metadata.get("image")
    warnings = []
    if metadata.get("seo_status") in ("Read from published page", "Some SEO fields were unavailable"):
        if not title:
            warnings.append("SEO title was not found in the published page metadata.")
        if not description:
            warnings.append("Meta description was not found in the published page metadata.")
    if "image" in metadata and not image:
        warnings.append("Shopify did not return a featured image.")
    if canonical and canonical.rstrip("/") != item["url"].rstrip("/"):
        warnings.append("The published canonical URL differs from the imported article URL.")
    if indexing.get("status") == "Not indexed":
        warnings.append("URL Inspection reported that this article is not indexed.")
    return {"seo_title_length": len(title) if title else None,
            "meta_description_length": len(description) if description else None,
            "has_featured_image": bool(image) if "image" in metadata else None,
            "seo_checked_at": metadata.get("seo_checked_at"),
            "indexing_status": indexing.get("status", "Unavailable"),
            "indexing": indexing, "warnings": warnings}


def prepublication_rows(item, rows):
    published = datetime.fromisoformat(item["published_at"])
    flagged = []
    for row in rows:
        if row["report"] != "ga4_views" or not row["metrics"].get("screenPageViews"):
            continue
        try:
            local_date = published.astimezone(ZoneInfo(row["timezone"])).date()
            if date.fromisoformat(row["day"]) < local_date:
                flagged.append({"day": row["day"], "views": row["metrics"]["screenPageViews"],
                                "publication_date_in_ga4_timezone": local_date.isoformat(),
                                "timezone": row["timezone"]})
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            continue
    return flagged


def facebook_latest(snapshots, kind):
    latest = max(snapshots, key=lambda row: row["observed_at"], default=None)
    metrics = latest["metrics"] if latest else {}
    states = metrics.get("availability", {})
    fields = (*POST_FIELDS, "outbound_clicks")
    if kind in {"reel", "video"}:
        fields = (*fields, *VIDEO_FIELDS)
    values = {key: metrics.get(key) for key in fields}
    clicks = metrics.get("post_clicks_by_type")
    categories = {label.strip().casefold().replace("_", " "): value for label, value in clicks.items()
                  if type(value) in (int, float)} if isinstance(clicks, dict) else {}
    for metric, label in (("link_clicks", "link click"), ("outbound_clicks", "outbound click")):
        values[metric] = categories.get(label + "s", categories.get(label))
    engagement = [values[key] for key in ("reactions", "comments", "shares")]
    views = values["post_media_view"]
    engagement_rate = ratio(sum(engagement), views) if all(isinstance(value, (int, float)) for value in engagement) \
        and isinstance(views, (int, float)) else None
    availability = {
        key: states.get(key, "Reported") if values[key] is not None else states.get(key, "Availability not recorded")
        for key in fields
    }
    if values["link_clicks"] is None:
        availability["link_clicks"] = states.get("post_clicks_by_type", "Click breakdown availability not recorded")
        if availability["link_clicks"] == "Reported" or isinstance(clicks, dict):
            availability["link_clicks"] = "No link-click category returned"
    if values["outbound_clicks"] is None:
        availability["outbound_clicks"] = ("No outbound-click category returned" if isinstance(clicks, dict)
                                            else states.get("post_clicks_by_type", "Click breakdown availability not recorded"))
    if latest is None:
        availability = {key: "No snapshot in selected range" for key in fields}
    return {"observed_at": latest["observed_at"] if latest else None,
            "api_version": latest["api_version"] if latest else None, "period": "lifetime",
            **values, "interactions_per_media_view": engagement_rate, "availability": availability}


def facebook_changes(snapshots):
    changes = []
    previous = {}
    for row in sorted(snapshots, key=lambda item: item["observed_at"]):
        for metric in CUMULATIVE_FIELDS:
            value = row["metrics"].get(metric)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            key = (row["api_version"], metric)
            if key in previous:
                before, old_value = previous[key]
                corrected = value < old_value
                changes.append({"metric": metric, "api_version": key[0], "from": before,
                                "to": row["observed_at"], "change": None if corrected else value - old_value,
                                "status": "Lifetime value decreased" if corrected else "Observed interval change"})
            previous[key] = row["observed_at"], value
    return changes
