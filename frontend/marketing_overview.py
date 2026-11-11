from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from backend.metrics import previous_period, summarize
from frontend.client import APIError, call
from frontend.commerce import show_commerce
from frontend.meta_ads import load_report as load_meta_report, totals as meta_totals
from frontend.views import coverage, number, period_rows, table


def google_rows(base, start, end):
    prior_start, prior_end = previous_period(start, end)
    for account in call(f"{base}/accounts"):
        row = {"Platform": "Google Ads", "Account": account["name"], "Currency": account["currency"]}
        try:
            report = call(f"{base}/accounts/{account['id']}/report", params={"start": str(start), "end": str(end)})
        except APIError as exc:
            yield {**row, "Status": str(exc)}
            continue
        current = period_rows(report, "campaigns", start, end)
        previous = period_rows(report, "campaigns", prior_start, prior_end)
        complete = coverage(account, start, end)
        currency = account["currency"]
        totals = summarize(current, currency) if current or complete else {}
        prior = summarize(previous, currency) if coverage(account, prior_start, prior_end) else {}
        yield {**row, "Status": "Imported" if complete else "Partial coverage" if current else "No imported data",
               "Spend": totals.get("spend"), "Previous spend": prior.get("spend"),
               "Impressions": totals.get("impressions"), "Clicks": totals.get("clicks"),
               "Previous clicks": prior.get("clicks"), "Google Ads conversions": totals.get("conversions"),
               "Google Ads conversion value": totals.get("value"), "Google Ads ROAS": totals.get("roas")}


def show_marketing_overview(base, mode, *, paid_only=False):
    st.title("All Paid" if paid_only else "Marketing Performance")
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    dates = st.date_input("Reporting dates", (today - timedelta(days=30), today - timedelta(days=1)),
                          max_value=today - timedelta(days=1), key=f"overview-dates:{base}:{paid_only}")
    if len(dates) != 2:
        st.info("Select a start and end date.")
        return
    start, end = dates
    if end - start > timedelta(days=365):
        st.info("Choose a range of up to 366 days.")
        return
    prior_start, prior_end = previous_period(start, end)
    st.caption(f"{start} to {end} · Previous period {prior_start} to {prior_end}. "
               "Google Ads dates follow each account's reporting time zone.")
    if not paid_only:
        show_commerce(base, start, end, prior_start, prior_end)

    st.subheader("Paid advertising")
    rows = list(google_rows(base, start, end))
    if not rows:
        rows.append({"Platform": "Google Ads", "Status": "No account connected"})
    try:
        meta = load_meta_report(base,start,end)
        if meta["account"]:
            account = meta["account"]
            metrics = meta_totals(meta)
            prior = meta_totals(load_meta_report(base,prior_start,prior_end))
            rows.append({"Platform":"Meta Ads", "Account":account["data"]["name"],
                         "Currency":account["data"]["currency"],
                         "Status":"Stale import" if meta["stale"] else "Imported" if meta["coverage"] else "Partial coverage",
                         "Spend":metrics["spend"],"Previous spend":prior["spend"],
                         "Impressions":metrics["impressions"],"Clicks":metrics["clicks"],
                         "Previous clicks":prior["clicks"],"Meta website purchases":metrics["website_purchases"],
                         "Meta website purchase value":metrics["website_purchase_value"],"Meta website ROAS":metrics["website_roas"]})
        else:
            rows.append({"Platform":"Meta Ads","Status":"No successful paid import yet"})
    except APIError as exc:
        rows.append({"Platform":"Meta Ads","Status":str(exc)})
    if mode == "live":
        try:
            tiktok = call(f"{base}/tiktok-ads/status")["configured"]
        except APIError:
            rows.append({"Platform": "TikTok Ads", "Status": "Connection status unavailable"})
        else:
            rows.append({"Platform": "TikTok Ads", "Status": "Report on demand" if tiktok else "Not connected"})
    table(rows)
    for currency in sorted({row["Currency"] for row in rows if row.get("Spend") is not None}):
        spend = sum(row["Spend"] for row in rows if row.get("Currency") == currency and row.get("Spend") is not None)
        st.metric(f"Available paid spend · {currency}", number(spend, "money", currency))
    clicks = [row["Clicks"] for row in rows if row.get("Clicks") is not None]
    if clicks:
        st.metric("Available paid campaign clicks", f"{sum(clicks):,}")
    st.caption("Google Ads conversions follow account conversion settings and can include actions other than purchases. "
               "Spend and clicks reflect the imported rows shown above.")
    if paid_only:
        return

    st.subheader("Organic marketing")
    try:
        catalog = call(f"{base}/content")
    except APIError as exc:
        st.warning(str(exc))
        return
    by_source = {row["source"]: row for row in catalog["sources"]}
    rows = []
    for label, source in (("Facebook Organic", "facebook"), ("Shopify Blog / Organic Search", "shopify")):
        published = [row for row in catalog["items"] if row["source"] == source and row.get("published_at")]
        current = sum(start.isoformat() <= row["published_at"][:10] <= end.isoformat() for row in published)
        prior = sum(prior_start.isoformat() <= row["published_at"][:10] <= prior_end.isoformat() for row in published)
        sync = by_source.get(source, {})
        rows.append({"Channel": label, "Content published (UTC)": current if published or sync else None,
                     "Previous period": prior if published or sync else None,
                     "Latest discovery": sync.get("finished_at"), "Attributed Shopify purchases": None})
    rows.append({"Channel": "Other / Direct / Unattributed", "Attributed Shopify purchases": None})
    table(rows)
    st.caption("Publication counts reflect imported items. Facebook views and reach are available under Organic Marketing.")
    st.markdown("**Shopify article performance**")
    organic = []
    for period, first, last in (("Selected", start, end), ("Previous", prior_start, prior_end)):
        try:
            result = call(f"{base}/content/organic-summary", params={"start": str(first), "end": str(last)})
        except APIError as exc:
            st.warning(f"{period} period · {exc}")
            continue
        for entry in result["rows"]:
            organic.append({"Period": period, "Source": "Search Console" if entry["report"] == "gsc_page" else "GA4",
                            "Property": entry["source_account"], "Currency": entry["currency"],
                            "Article views": entry["article_views"], "Article landing sessions": entry["landing_sessions"],
                            "GA4 purchases in article landing sessions": entry["landing_purchases"],
                            "GA4 revenue in article landing sessions": entry["landing_revenue"],
                            "Search clicks": entry["search_clicks"], "Search impressions": entry["search_impressions"]})
    table(organic)
    st.caption("GA4 landing-session outcomes describe sessions that began on an article. "
               "They do not identify unique Shopify orders or establish that the article caused a purchase.")
