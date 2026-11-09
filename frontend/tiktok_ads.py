from collections import defaultdict
from datetime import datetime, timedelta, timezone

import streamlit as st

from backend.metrics import ratio
from frontend.client import call
from frontend.views import table

TIKTOK_NOTE = ("Dates follow the TikTok advertiser account time zone. Conversions use TikTok attribution "
               "and may count different events from Google Ads and GA4.")


def campaign_totals(rows):
    campaigns = defaultdict(list)
    for row in rows:
        campaigns[row["campaign_id"], row["currency"]].append(row)
    totals = []
    for (campaign_id, currency), entries in campaigns.items():
        metrics = {
            key: sum(entry[key] for entry in entries) if all(entry[key] is not None for entry in entries) else None
            for key in ("spend", "impressions", "clicks", "conversion")
        }
        totals.append({
            "campaign_id": campaign_id, "campaign": max(entries, key=lambda row: row["day"])["campaign_name"],
            "currency": currency, **metrics, "ctr": ratio(metrics["clicks"], metrics["impressions"]),
            "average_cpc": ratio(metrics["spend"], metrics["clicks"]),
        })
    return totals


def show_tiktok_ads(base):
    st.title("TikTok Ads")
    st.caption(TIKTOK_NOTE)
    status = call(f"{base}/tiktok-ads/status")
    if not status["configured"]:
        st.info("TikTok Ads is not connected for this organization. Configure an authorized TikTok advertiser on the backend.")
        return
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    dates = st.date_input("TikTok Ads date range", (end - timedelta(days=29), end), max_value=end,
                          key=f"tiktok-dates:{base}")
    if len(dates) != 2:
        return
    start, end = dates
    if end - start > timedelta(days=365):
        st.info("Choose a range of up to 366 days.")
        return
    key = f"tiktok-report:{base}"
    if st.button("Load TikTok report", type="primary"):
        st.session_state.pop(key, None)
        with st.spinner("Loading TikTok Ads performance…"):
            result = call(f"{base}/tiktok-ads/report", params={"start": str(start), "end": str(end)}, timeout=90)
            st.session_state[key] = (dates, result["rows"])
    saved = st.session_state.get(key)
    if saved and saved[0] == dates:
        rows = saved[1]
        st.subheader("Campaign totals")
        table(campaign_totals(rows))
        st.subheader("Daily campaign performance")
        table(rows)
