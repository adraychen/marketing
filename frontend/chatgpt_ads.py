from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.express as px
import streamlit as st
from backend.metrics import ratio
from frontend.client import call, APIError
from frontend.views import table

ADDITIVE = ("spend", "impressions", "clicks", "conversions", "order_created_attributed_sales")
LABELS = {"spend": "Spend", "impressions": "Impressions", "clicks": "Clicks", "conversions": "ChatGPT goal conversions",
          "order_created_attributed_sales": "ChatGPT attributed sales"}


def totals(rows):
    result = {key: sum(r[key] for r in rows) if rows and all(r.get(key) is not None for r in rows) else None for key in ADDITIVE}
    result.update(calculated_ctr=ratio(result["clicks"], result["impressions"]),
                  calculated_cpc=ratio(result["spend"], result["clicks"]),
                  calculated_cpa=ratio(result["spend"], result["conversions"]),
                  calculated_roas=ratio(result["order_created_attributed_sales"], result["spend"]))
    return result


def load(base, start, end):
    return call(f"{base}/chatgpt-ads/report", params={"start": str(start), "end": str(end)})


def sync_panel(base, mode, role):
    state = call(f"{base}/chatgpt-ads/status")
    st.subheader("ChatGPT Ads connection")
    st.caption("Read-only reporting. The Advertiser API key stays in backend and sync-job environment secrets.")
    st.code("OPENAI_ADS_API_KEY=<secret key value>\nOPENAI_ADS_ORGANIZATION_ID=" + base.rsplit("/", 1)[-1] +
            "\nOPENAI_ADS_LOOKBACK_DAYS=90", language=None)
    if not state["schema_ready"]:
        st.info("Apply migration 20261118000100_chatgpt_ads.sql before syncing.")
    if not state["configured"]:
        st.info("Add the Advertiser API key and organization ID to the server environment, then redeploy.")
    if st.button("Sync ChatGPT Ads", disabled=mode != "live" or role != "admin" or not state["configured"] or not state["schema_ready"]):
        try:
            with st.spinner("Importing daily ad performance…"):
                result = call(f"{base}/chatgpt-ads/sync", method="POST", timeout=300)
            st.success(f"Imported {result['daily_ad_rows']} daily ad rows.")
            st.rerun()
        except APIError as exc:
            st.error(str(exc))
    table(state["accounts"])
    table(state["runs"])


def add_report(report, data, campaign_ids=None):
    rows = [r for r in data["daily"] if campaign_ids is None or r["campaign_id"] in campaign_ids]
    report.note(data["policy"])
    report.table("ChatGPT Ads coverage", [{"source": "OpenAI Advertiser API", "account": data.get("account"),
        "synced_at": data.get("synced_at"), "complete": data["coverage"], "stale": data["stale"],
        "attribution": data["attribution"], "state": "available" if rows else "unavailable"}])
    report.table("ChatGPT Ads overview", [totals(rows)])
    from frontend.reports import paid_dashboard_row
    report.table("ChatGPT Ads dashboard metrics", [{**paid_dashboard_row("ChatGPT Ads", totals(rows), "conversions",
        (data.get("account") or {}).get("currency_code")),
        "Conversion definition": "ChatGPT goal conversions (not necessarily purchases)",
        "Data status": "Needs refresh" if data["stale"] else "Available" if data["coverage"] else "Partial period"}])
    report.table("ChatGPT Ads campaign performance", [{"campaign_id": key, **totals([r for r in rows if r["campaign_id"] == key])}
                                                     for key in sorted({r["campaign_id"] for r in rows})])
    report.table("ChatGPT Ads daily ad performance", rows)


def show(base):
    st.title("ChatGPT Ads")
    state = call(f"{base}/chatgpt-ads/status")
    if not state["accounts"]:
        st.info("Connect and sync ChatGPT Ads in Connections & Sync first.")
        return
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    dates = st.date_input("ChatGPT reporting dates", (today-timedelta(days=30), today-timedelta(days=1)), key=f"chatgpt-dates:{base}")
    if len(dates) != 2 or dates[0] > dates[1] or (dates[1]-dates[0]).days >= 365:
        return
    data = load(base, *dates)
    options = sorted({r["campaign_id"] for r in data["daily"]})
    selected = st.multiselect("ChatGPT campaigns", options, default=options)
    rows = [r for r in data["daily"] if r["campaign_id"] in selected]
    st.caption(data["policy"])
    st.write({"account": data.get("account"), "attribution": data["attribution"], "last_success": data.get("synced_at")})
    if not data["coverage"] or data["stale"]:
        st.warning("This report has incomplete coverage or is over 48 hours old. Sync to refresh it.")
    if state["runs"] and state["runs"][0]["status"] == "failed":
        st.warning("Latest sync failed. Displaying the last successful import.")
    values = totals(rows)
    currency = (data.get("account") or {}).get("currency_code", "")
    for column, key in zip(st.columns(3), ("spend", "clicks", "conversions")):
        label = f"Spend · {currency}" if key == "spend" else LABELS[key]
        column.metric(label, "Not available" if values[key] is None else f"{values[key]:,.2f}")
    st.subheader("Daily ad performance")
    st.caption("Each point is that day's value, not a running total. Missing dates are not filled with zero.")
    if rows:
        frame = pd.DataFrame(rows)
        frame["day"] = pd.to_datetime(frame["day"])
        columns = st.columns(2)
        for index, key in enumerate(ADDITIVE):
            if frame[key].notna().any():
                chart = px.line(frame.sort_values("day"), x="day", y=key, color="ad_id", markers=True,
                                hover_data=["ad_name", "campaign_id", "currency"],
                                labels={key: LABELS[key] + (f" ({currency})" if key in ("spend", "order_created_attributed_sales") else ""), "day": "Date"})
                columns[index % 2].plotly_chart(chart, use_container_width=True)
            else:
                columns[index % 2].info(f"{LABELS[key]} unavailable for this selection.")
    from frontend.reports import Report
    report = Report()
    report.note(f"Requested dates: {dates[0]} to {dates[1]}.")
    add_report(report, data, selected)
    blob, extension, mime = report.export()
    st.download_button("Download ChatGPT Ads report", blob, file_name=f"chatgpt-ads-{dates[0]}-{dates[1]}.{extension}", mime=mime)
