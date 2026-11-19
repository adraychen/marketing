"""Headline organic metrics and the full blog report share the existing imports."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import streamlit as st
from backend.metrics import previous_period
from frontend.client import call, APIError
from frontend.views import table


def blog_rows(base, start, end):
    return call(f"{base}/content/organic-summary", params={"start": str(start), "end": str(end)})["rows"]


def headline(rows, report, field):
    matches = [row for row in rows if row["report"] == report]
    # Different GA4/GSC properties may measure the same traffic. Never add them.
    if len(matches) != 1 or matches[0].get(field) is None:
        return "—"
    return f"{matches[0][field]:,.0f}"


def show_overview(base, catalog, start, end):
    runs = {row["source"]: row for row in catalog["sources"]}
    try:
        blog = blog_rows(base, start, end)
        blog_status = "Reported data" if blog else "No reported data"
    except APIError:
        blog, blog_status = [], "Unavailable"
    audience = "Followers: —"
    fb_status = "Unavailable"
    try:
        data = call(f"{base}/content/facebook-analytics", params={"start": str(start), "end": str(end)})
        followers = [r for r in data["page"] if r["metric"] == "followers_count" and r.get("value") is not None
                     and str(start) <= r["day"] <= str(end)]
        if followers and len({r["page_id"] for r in followers}) == 1:
            latest = max(followers, key=lambda r: (r["day"], r["collected_at"]))
            audience = f"{latest['value']:,.0f} Page followers · observed {latest['day']}"
            fb_status = "Latest observation"
    except APIError:
        pass
    result = []
    for source, label, metrics, status in (
        ("facebook", "Facebook Organic", audience, fb_status),
        ("shopify", "Shopify Blog", f"{headline(blog, 'ga4_views', 'article_views')} article views · "
         f"{headline(blog, 'ga4_landing', 'landing_sessions')} landing sessions · "
         f"{headline(blog, 'gsc_page', 'search_clicks')} search clicks", blog_status),
    ):
        items = [r for r in catalog["items"] if r["source"] == source and r.get("published_at")]
        count = sum(str(start) <= r["published_at"][:10] <= str(end) for r in items)
        published = f"{count:,}" if items or runs.get(source, {}).get("status") == "succeeded" else "—"
        result.append({"Channel": label, "Content published": published, "Key metrics": metrics, "Data status": status})
    table(result)
    st.caption("Published counts use UTC. Followers are a Page audience snapshot, not period reach. "
               "Blog metrics cover reported dates; — means unavailable or multiple reporting properties. "
               "Open Organic Marketing for post, article and source details.")


def show_blog_details(base):
    st.subheader("Shopify blog performance")
    st.caption("All blog articles, independent of the campaign filter. Views, landing sessions and search results are separate measurements.")
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    dates = st.date_input("Blog performance dates", (today-timedelta(days=30), today-timedelta(days=1)),
                          max_value=today, key=f"blog-summary:{base}")
    if len(dates) != 2 or dates[0] > dates[1] or (dates[1]-dates[0]).days > 365:
        return
    start, end = dates
    prior_start, prior_end = previous_period(start, end)
    rows = []
    for period, first, last in (("Selected", start, end), ("Previous", prior_start, prior_end)):
        try:
            entries = blog_rows(base, first, last)
        except APIError as exc:
            st.warning(f"{period} period · {exc}")
            continue
        rows.extend({"Period": period, "Start": str(first), "End": str(last),
                     "Source": "Search Console" if r["report"] == "gsc_page" else "GA4",
                     "Property": r["source_account"], "Currency": r["currency"],
                     "Article views": r["article_views"], "Article landing sessions": r["landing_sessions"],
                     "GA4 purchases in article landing sessions": r["landing_purchases"],
                     "GA4 revenue in article landing sessions": r["landing_revenue"],
                     "Search clicks": r["search_clicks"], "Search impressions": r["search_impressions"]} for r in entries)
    table(rows)
    st.caption("GA4 landing-session outcomes are not unique Shopify orders or proof that an article caused a purchase.")
