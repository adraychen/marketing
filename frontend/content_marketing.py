import json
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from frontend.client import call
from frontend.views import table

SOURCE_LABELS = {
    "shopify": "Shopify articles", "facebook": "Facebook posts", "ga4": "Google Analytics",
    "search_console": "Search Console", "facebook_insights": "Facebook post insights",
}
GA_LABELS = {
    "screenPageViews": "Article views", "activeUsers": "Daily active users", "sessions": "Landing sessions",
    "engagedSessions": "Engaged sessions", "ecommercePurchases": "Purchases", "purchaseRevenue": "Purchase revenue",
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
        keys = [key for key in GA_LABELS if key != "activeUsers" and any(key in row["metrics"] for row in entries)]
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
        impressions = sum(row["metrics"]["impressions"] for row in entries)
        clicks = sum(row["metrics"]["clicks"] for row in entries)
        position = sum(row["metrics"]["position"] * row["metrics"]["impressions"] for row in entries)
        cols = st.columns(4)
        cols[0].metric("Search clicks", f"{clicks:,}")
        cols[1].metric("Impressions", f"{impressions:,}")
        cols[2].metric("CTR", f"{clicks / impressions:.2%}" if impressions else "—")
        cols[3].metric("Avg. position", f"{position / impressions:.1f}" if impressions else "—")
        st.line_chart(pd.DataFrame([{"Date": row["day"], "Clicks": row["metrics"]["clicks"],
                                     "Impressions": row["metrics"]["impressions"]} for row in entries]),
                      x="Date")
    queries = [row for row in daily if row["report"] == "gsc_query"]
    for (account,), entries in grouped(queries, "source_account").items():
        totals = defaultdict(lambda: {"Clicks": 0, "Impressions": 0})
        for row in entries:
            item = totals[row["segment"]]
            item["Clicks"] += row["metrics"]["clicks"]
            item["Impressions"] += row["metrics"]["impressions"]
        st.caption(f"Top reported queries · {account} · Some queries may be omitted.")
        table([{"Query": key, **value} for key, value in
               sorted(totals.items(), key=lambda pair: pair[1]["Clicks"], reverse=True)[:20]])


def show_article(details):
    daily = details["daily"]
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
    search_report(daily)


def show_facebook(details):
    rows = details["snapshots"]
    if not rows:
        st.caption("No Facebook Insights observations for these dates.")
        return
    st.caption("Observed lifetime counts. Each row is the latest observation on that day and API version. "
               "Unique viewers and lifetime counts should not be summed across dates.")
    table([{"Observed": row["observed_at"], "API": row["api_version"],
            **{label: row["metrics"].get(key) if isinstance(row["metrics"].get(key), (float, int)) else None
               for key, label in META_LABELS.items()}}
           for row in rows])
    for key, label in (("post_media_view", "media views"), ("post_clicks_by_type", "clicks")):
        for row in reversed(rows):
            metric = row["metrics"].get(key)
            if isinstance(metric, dict) and metric:
                st.caption(f"Latest available Meta breakdown for {label} · {row['observed_day']}")
                table([{"Meta category": category, "Value": value} for category, value in metric.items()])
                break


def show_content_marketing(base):
    st.title("Content Marketing")
    st.caption("Track published Shopify articles and Facebook posts alongside store and search performance.")
    route = f"{base}/content"
    catalog = call(route)
    status = {item["source"]: item for item in catalog["sources"]}
    with st.expander("Source status", expanded=not catalog["items"]):
        table([{"Source": label, "Status": status.get(key, {}).get("status", "No sync recorded"),
                "Last run": status.get(key, {}).get("finished_at"),
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
        show_article(details)
    else:
        show_facebook(details)
    if details["links"]:
        st.markdown("**Post links and campaign tags**")
        table([{"Article": row["article"], "Facebook post": row["post"], "Link": row["url"], **row["utm"]}
               for row in details["links"]])
