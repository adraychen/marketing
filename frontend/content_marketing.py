import json
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from backend.metrics import ratio
from frontend.client import call
from frontend.views import table
from frontend.visibility import facebook_changes, facebook_latest, prepublication_rows, search_queries, search_totals, seo_review

SOURCE_LABELS = {
    "shopify": "Shopify articles", "facebook": "Facebook posts", "ga4": "Google Analytics",
    "ga4_traffic": "Google Analytics traffic sources",
    "search_console": "Search Console", "facebook_insights": "Facebook post insights",
    "search_console_inspection": "Search Console URL Inspection",
}
GA_LABELS = {
    "screenPageViews": "Article views", "activeUsers": "Daily active users", "sessions": "Landing sessions",
    "engagedSessions": "Engaged sessions", "ecommercePurchases": "Purchases", "purchaseRevenue": "Purchase revenue",
    "userEngagementDuration": "Engagement time in seconds", "engagementRate": "Engagement rate",
    "newUsers": "Daily new users",
}
META_LABELS = {
    "post_media_view": "Media views", "post_total_media_view_unique": "Unique media viewers",
    "post_clicks": "Post clicks", "reactions": "Reactions", "comments": "Comments", "shares": "Shares",
}


def external_link(url, label):
    if url and urlsplit(url).scheme in {"http", "https"}:
        st.link_button(label, url)


def ga_report(title, rows, *, totals=True):
    st.markdown(f"**{title}**")
    if not rows:
        st.caption("No data returned for these dates.")
        return
    for (account, currency, timezone), entries in grouped(rows, "source_account", "currency", "timezone").items():
        st.caption(" · ".join(filter(None, (f"GA4 property {account}", timezone, currency))))
        keys = [key for key in GA_LABELS if key not in {"activeUsers", "newUsers", "engagementRate"}
                and any(key in row["metrics"] for row in entries)]
        if totals and keys:
            for column, key in zip(st.columns(len(keys)), keys):
                value = sum(row["metrics"].get(key, 0) for row in entries)
                formatted = f"{value:,.2f} {currency or ''}".strip() if key == "purchaseRevenue" else f"{value:,.0f}"
                column.metric(GA_LABELS[key], formatted)
        table([{"Date": row["day"], **{GA_LABELS[key]: value for key, value in row["metrics"].items()}}
               for row in entries])


def grouped(rows, *keys):
    result = defaultdict(list)
    for row in rows:
        result[tuple(row[key] for key in keys)].append(row)
    return result


def search_report(daily):
    st.markdown("**Google organic search**")
    pages = [row for row in daily if row["report"] == "gsc_page"]
    if not pages:
        st.caption("No page-level search data returned for these dates.")
    for (account,), entries in grouped(pages, "source_account").items():
        st.caption(f"{account} · Search Console dates")
        metrics = search_totals(entries)
        cols = st.columns(4)
        cols[0].metric("Search clicks", f"{metrics['clicks']:,}")
        cols[1].metric("Impressions", f"{metrics['impressions']:,}")
        cols[2].metric("CTR", f"{metrics['ctr']:.2%}" if metrics["ctr"] is not None else "—")
        cols[3].metric("Avg. position", f"{metrics['position']:.1f}" if metrics["position"] is not None else "—")
        st.line_chart(pd.DataFrame([{"Date": row["day"], "Clicks": row["metrics"]["clicks"],
                                     "Impressions": row["metrics"]["impressions"]} for row in entries]),
                      x="Date")
    queries = search_queries(daily)
    if queries:
        st.caption("Top reported queries. Some queries are omitted and query totals may differ from page totals.")
        table(queries[:20])


def show_article(item, details):
    daily = details["daily"]
    metadata = details.get("metadata", {})
    seo = seo_review(item, metadata)
    with st.expander("SEO and indexing", expanded=True):
        table([{**{key: metadata.get(key) for key in ("seo_title", "meta_description", "canonical_url",
                                                       "social_title", "social_description", "tags")},
                **{key: value for key, value in seo.items() if key not in {"indexing", "warnings"}}}])
        for warning in seo["warnings"]:
            st.warning(warning)
        indexing = seo["indexing"]
        if indexing:
            table([indexing])
        external_link(indexing.get("inspection_url") or indexing.get("manual_url")
                      or "https://search.google.com/search-console", "Open Search Console URL Inspection")
    for row in prepublication_rows(item, daily):
        st.warning(f"GA4 reported {row['views']:g} views on {row['day']} before the recorded publication date "
                   f"{row['publication_date_in_ga4_timezone']} in {row['timezone']}.")
    ga_report("Article readership", [row for row in daily if row["report"] == "ga4_views"])
    st.caption("Active users are shown by day because a person can return on multiple days.")
    ga_report("Sessions that began on this article", [row for row in daily if row["report"] == "ga4_landing"])
    tagged = [row for row in daily if row["report"] == "ga4_facebook"]
    if not tagged:
        st.caption("No Facebook-tagged landing data returned for these dates.")
    for (segment,), entries in grouped(tagged, "segment").items():
        utm = json.loads(segment)
        ga_report("Facebook-tagged sessions", entries)
        st.text(" / ".join(utm.values()))
    st.caption("Purchases and revenue are measured for sessions that began on this article. Tagged reports can overlap.")
    st.markdown("**Landing traffic by source and medium**")
    table([{"Date": row["day"], "Property": row["source_account"], **json.loads(row["segment"]), **row["metrics"],
            "Average engagement seconds per session": ratio(row["metrics"].get("userEngagementDuration"),
                                                             row["metrics"].get("sessions"))}
           for row in daily if row["report"] == "ga4_traffic"])
    search_report(daily)


def show_facebook(details):
    rows = details["snapshots"]
    metadata = details.get("metadata", {})
    kind = metadata.get("content_type", "unknown")
    st.caption(f"Facebook Page content · {kind}")
    if metadata.get("message"):
        st.text(metadata["message"])
    external_link(metadata.get("media_reference"), "Open media preview")
    st.markdown("**Latest lifetime observation in this range**")
    latest = facebook_latest(rows, kind)
    table([{"Metric": key, "Value": latest[key], "Availability": state}
           for key, state in latest["availability"].items()])
    st.caption("Reach and impressions are unavailable in this import. Unique media viewers are shown separately.")
    if not rows:
        st.caption("No Facebook Insights observations for these dates.")
        return
    st.caption("Observed lifetime counts. Identical repeat observations on the same date are deduplicated. "
               "Unique viewers and lifetime counts should not be summed across dates.")
    table([{"Observed": row["observed_at"], "API": row["api_version"],
            **{label: row["metrics"].get(key) if isinstance(row["metrics"].get(key), (float, int)) else None
               for key, label in META_LABELS.items()}}
           for row in rows])
    st.markdown("**Changes between observations**")
    st.caption("Interval changes are not exact daily performance. Decreases may be corrections.")
    table(facebook_changes(rows))
    for key, label in (("post_media_view", "media views"), ("post_clicks_by_type", "clicks")):
        for row in reversed(rows):
            metric = row["metrics"].get(key)
            if isinstance(metric, dict) and metric:
                st.caption(f"Latest available Meta breakdown for {label} · {row['observed_day']}")
                table([{"Meta category": category, "Value": value} for category, value in metric.items()])
                break


def show_visibility_summary(route, items):
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    dates = st.date_input("Visibility summary date range", (today - timedelta(days=29), today),
                          max_value=today, key=f"visibility:{route}")
    if len(dates) != 2 or dates[1] - dates[0] > timedelta(days=365):
        return
    params = {"start": str(dates[0]), "end": str(dates[1])}
    key = f"content-summary:{route}"
    scope = (tuple(item["id"] for item in items), dates)
    if st.button("Load visibility summary"):
        st.session_state[key] = scope, [(item, call(f"{route}/items/{item['id']}", params=params)) for item in items]
    saved = st.session_state.get(key)
    if not saved or saved[0] != scope:
        return
    articles, facebook = [], []
    for item, details in saved[1]:
        metadata = details.get("metadata", {})
        if item["source"] == "shopify":
            daily = details["daily"]
            for (source,), rows in grouped([row for row in daily if row["report"] == "ga4_views"],
                                           "source_account").items():
                articles.append({"source": "GA4", "property": source, "article": item["title"],
                                 "views": sum(row["metrics"]["screenPageViews"] for row in rows)})
            pages = [row for row in daily if row["report"] == "gsc_page"]
            for (source,), rows in grouped(pages, "source_account").items():
                articles.append({"source": "Search Console", "property": source,
                                 "article": item["title"], **search_totals(rows)})
            articles.append({"source": "Coverage", "article": item["title"], "missing_analytics": not daily,
                             "indexing": (metadata.get("indexing") or {}).get("status", "Unavailable")})
        else:
            kind = metadata.get("content_type", "unknown")
            latest = facebook_latest(details["snapshots"], kind)
            facebook.append({"content": item["title"], "type": kind,
                             **{field: latest[field] for field in ("observed_at", "post_media_view",
                                                                   "reactions", "comments", "shares", "post_clicks")},
                             "views_unavailable": latest["post_media_view"] is None})
    st.subheader("Shopify visibility summary")
    table(articles)
    coverage_rows = [row for row in articles if row["source"] == "Coverage"]
    st.caption(f"{sum(row['indexing'] == 'Indexed' for row in coverage_rows)} verified indexed · "
               f"{sum(row['missing_analytics'] for row in coverage_rows)} articles without analytics rows")
    for (source, prop), rows in grouped([row for row in articles if row["source"] != "Coverage"],
                                        "source", "property").items():
        if source == "GA4":
            st.metric(f"Article views · {prop}", f"{sum(row['views'] for row in rows):,}")
        else:
            impressions = sum(row["impressions"] for row in rows)
            clicks = sum(row["clicks"] for row in rows)
            columns = st.columns(3)
            columns[0].metric(f"Search impressions · {prop}", f"{impressions:,}")
            columns[1].metric("Search clicks", f"{clicks:,}")
            columns[2].metric("Search CTR", f"{clicks / impressions:.2%}" if impressions else "—")
    st.subheader("Facebook visibility summary")
    st.caption(f"{len(facebook)} items · {sum(row['type'] == 'reel' for row in facebook)} identified reels · "
               f"{sum(row['views_unavailable'] for row in facebook)} without reported media views. "
               "Values are the latest lifetime observations in the range, shown per item.")
    table(facebook)


def show_content_marketing(base):
    st.title("Content Marketing")
    st.caption("Track published Shopify articles and Facebook posts alongside store and search performance.")
    route = f"{base}/content"
    catalog = call(route)
    status = {item["source"]: item for item in catalog["sources"]}
    with st.expander("Source status", expanded=not catalog["items"]):
        table([{"Source": label, "Status": status.get(key, {}).get("status", "No sync recorded"),
                "Last run": status.get(key, {}).get("finished_at"),
                "Last success": status.get(key, {}).get("last_success_at"),
                "Records": status.get(key, {}).get("records"), "Details": status.get(key, {}).get("message")}
               for key, label in SOURCE_LABELS.items()])
    if not catalog["items"]:
        st.info("No published content has been imported into this workspace.")
        return

    campaigns = {item["slug"]: item["name"] for item in catalog["campaigns"]}
    campaign = st.selectbox("Content campaign", [*campaigns, ""],
                            format_func=lambda key: campaigns.get(key, "All discovered content"))
    items = [item for item in catalog["items"] if not campaign or campaign in item["campaigns"]]
    library, performance, associations = st.tabs(("Overview", "Content performance", "Linked products & ads"))
    with library:
        table([{"Title": item["title"], "Source": SOURCE_LABELS[item["source"]], "Published": item["published_at"],
                "Status": "Published" if item["active"] else "No longer listed"} for item in items])
        if items:
            show_visibility_summary(route, items)
    with performance:
        if not items:
            st.info("No items are linked to this campaign yet.")
        else:
            show_item_performance(route, items)
    with associations:
        if not campaign:
            st.info("Select a content campaign to see its product and Google Ads associations.")
        else:
            links = call(f"{route}/campaigns/{campaign}/links")
            st.markdown("**Shopify products**")
            table(links["products"])
            st.markdown("**Related Google Ads campaigns**")
            table(links["ads"])


def show_item_performance(route, items):
    by_id = {item["id"]: item for item in items}
    chosen = st.selectbox("Article or post", list(by_id), format_func=lambda key: by_id[key]["title"])
    item = by_id[chosen]
    external_link(item["url"], "Open published content")
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    dates = st.date_input("Date range", (today - timedelta(days=29), today), max_value=today, key=f"content:{route}")
    if len(dates) != 2:
        return
    start, end = dates
    details = call(f"{route}/items/{chosen}", params={"start": start.isoformat(), "end": end.isoformat()})
    if item["source"] == "shopify":
        show_article(item, details)
    else:
        show_facebook(details)
    if details["links"]:
        st.markdown("**Post links and campaign tags**")
        table([{"Article": row["article"], "Facebook post": row["post"], "Link": row["url"], **row["utm"]}
               for row in details["links"]])
