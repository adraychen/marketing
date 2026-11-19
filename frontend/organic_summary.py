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


def post_total(rows, fields):
    # Missing observations must not become zeros or misleading partial totals.
    values = [(row.get("metrics") or {}).get(field) for row in rows for field in fields]
    if not values or any(type(value) not in (int, float) for value in values):
        return "—"
    return f"{sum(values):,.0f}"


def show_overview(base, catalog, start, end):
    runs = {row["source"]: row for row in catalog["sources"]}

    def published(source):
        items = [r for r in catalog["items"] if r["source"] == source and r.get("published_at")]
        count = sum(str(start) <= r["published_at"][:10] <= str(end) for r in items)
        return f"{count:,}" if items or runs.get(source, {}).get("status") == "succeeded" else "—"

    try:
        blog = blog_rows(base, start, end)
        blog_status = "Reported data" if blog else "No reported data"
    except APIError:
        blog, blog_status = [], "Unavailable"
    st.markdown("#### Shopify Blog")
    table([{"Articles published": published("shopify"),
            "Article views": headline(blog, "ga4_views", "article_views"),
            "Landing sessions": headline(blog, "ga4_landing", "landing_sessions"),
            "Google search impressions": headline(blog, "gsc_page", "search_impressions"),
            "Google search clicks": headline(blog, "gsc_page", "search_clicks"),
            "Data status": blog_status}])
    st.caption("GA4 article views and landing sessions; Search Console search impressions and clicks. "
               "Metrics cover reported dates in the selected period. Published counts use UTC. "
               "— means unavailable or multiple reporting properties. Article details are under Organic Marketing → Shopify Blog.")

    try:
        posts = call(f"{base}/content/organic-post-summary",
                     params={"start": str(start), "end": str(end)})["rows"]
        fb_status = "Lifetime observations" if posts else "No observations"
    except APIError:
        posts, fb_status = [], "Unavailable"
    fields = {"Media views": ("post_media_view",),
              "Interactions": ("reactions", "comments", "shares"),
              "Post clicks": ("post_clicks",)}
    totals = {label: post_total(posts, keys) for label, keys in fields.items()}
    if posts and "—" in totals.values():
        fb_status = "Partial / unavailable metrics"
    st.markdown("#### Meta posts and Reels")
    table([{"Platform": "Facebook", "Content published": published("facebook"),
            **totals, "Data status": fb_status},
           {"Platform": "Instagram", "Content published": "—",
            **{label: "—" for label in fields}, "Data status": "Untracked"}])
    st.caption("Facebook metrics are lifetime totals for imported posts and Reels published in the selected UTC period, "
               "using each item's latest observation within that period—not activity during the period. "
               "Interactions = reactions + comments + shares; a total is unavailable if any included item lacks a component. "
               "Post clicks include more than website link clicks. Views may include paid distribution; they are not impressions or unique reach.")
    st.caption("Instagram content tracking is not connected. Instagram referrals are not attributed to owned posts or Reels. "
               "Post details and measurement limitations remain under Organic Marketing.")


def article_row(item, daily):
    result = {"Title": item["title"], "Published": item.get("published_at")}
    for label, report, metric in (
        ("Article views", "ga4_views", "screenPageViews"),
        ("Article landing sessions", "ga4_landing", "sessions"),
        ("Purchases in article landing sessions", "ga4_landing", "ecommercePurchases"),
        ("Revenue in article landing sessions", "ga4_landing", "purchaseRevenue"),
        ("Google search clicks", "gsc_page", "clicks"),
        ("Google search impressions", "gsc_page", "impressions"),
    ):
        rows = [r for r in daily if r["report"] == report]
        scopes = {(r["source_account"], r.get("currency"), r.get("timezone")) for r in rows}
        values = [r["metrics"].get(metric) for r in rows]
        if len(scopes) != 1 or not values or any(v is None for v in values):
            result[label] = "—"
        elif metric == "purchaseRevenue":
            currency = rows[0].get("currency")
            result[label] = f"{currency} {sum(values):,.2f}" if currency else "—"
        else:
            result[label] = f"{sum(values):,.0f}"
    return result


def show_article_table(route, items):
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    dates = st.date_input("Article reporting dates", (today-timedelta(days=30), today-timedelta(days=1)),
                          max_value=today, key=f"article-table:{route}")
    if len(dates) != 2 or not 0 <= (dates[1]-dates[0]).days <= 365:
        return
    rows = []
    for item in items:
        try:
            details = call(f"{route}/items/{item['id']}", params={"start": str(dates[0]), "end": str(dates[1])})
            daily = details["daily"]
        except APIError:
            daily = []
            st.warning(f"Performance unavailable for {item['title']}.")
        rows.append(article_row(item, daily))
    table(rows)
    st.caption("All imported articles. Published timestamps use UTC; metrics cover reported dates in the selected period. "
               "— means unavailable or multiple reporting properties, not zero.")
    st.caption("Google search clicks are clicks from Google organic search results (Search Console). Article views are GA4 page views "
               "from all sources, including repeat views. Google search impressions count appearances in Google search results.")
    st.caption("Landing sessions begin on the article. Purchases and revenue are GA4 outcomes in those sessions, "
               "not canonical Shopify orders or proof that the article caused a purchase. Source reporting time zones may differ.")


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
                     "Google search clicks": r["search_clicks"], "Google search impressions": r["search_impressions"]} for r in entries)
    table(rows)
    st.caption("GA4 landing-session outcomes are not unique Shopify orders or proof that an article caused a purchase.")
