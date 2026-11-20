"""Reddit uses the existing paid-page controls, tables, charts and report writer."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

from backend.reddit_metrics import totals
from backend.connectors.reddit_ads import EVENTS
from frontend.client import call, APIError
from frontend.views import number, table


def load(base, start, end):
    return call(f"{base}/reddit-ads/report", params={"start": str(start), "end": str(end)})


def state(data):
    return "Definition conflict" if not data["compatible"] else "Needs refresh" if data["stale"] else "Available" if data["coverage"] else "Partial period"


def account_totals(data, rows, conversion="purchases"):
    result = totals(rows, conversion)
    return result if data["compatible"] else {key: None for key in result}


def summary_rows(report):
    rows = []
    for data in report["accounts"]:
        account = data["account"]["data"]
        values = account_totals(data, data["daily"])
        rows.append({"Platform": "Reddit Ads", "Account": account["name"], "Currency": account["currency"],
            "Status": "Definition conflict" if not data["compatible"] else "Stale import" if data["stale"] else "Imported" if data["coverage"] else "Partial coverage",
            "Spend": values["spend"], "Impressions": values["impressions"], "Clicks": values["clicks"],
            "Reddit purchases": values["purchases"]})
    return rows


def campaign_options(data):
    return [(f"{account['account']['id']}:{campaign['id']}",
             f"{account['account']['data']['name']} · {campaign.get('name') or campaign['id']}")
            for account in data["accounts"] for campaign in account["campaigns"]]


def scoped_metadata(data, campaign_ids):
    groups = [g for g in data["ad_groups"] if g.get("campaign_id") in campaign_ids]
    group_ids = {g["id"] for g in groups}
    return {"campaigns": [c for c in data["campaigns"] if c["id"] in campaign_ids], "ad_groups": groups,
            "ads": [a for a in data["ads"] if a.get("ad_group_id") in group_ids or a.get("campaign_id") in campaign_ids]}


def performance(data, level, rows, conversion="purchases", campaign_ids=None):
    key = {"campaigns": "campaign_id", "ad_groups": "ad_group_id", "ads": "ad_id"}[level]
    ids = {r["campaign_id"] for r in rows} if campaign_ids is None else set(campaign_ids)
    return [{"ID": item["id"], "Name": item.get("name"), "Status": item.get("effective_status"),
             **account_totals(data, [r for r in rows if r[key] == item["id"]], conversion)}
            for item in scoped_metadata(data, ids)[level]]


def add_report(report, data, selected=None, conversion="purchases"):
    from frontend.reports import paid_dashboard_row
    report.note(data["policy"])
    report.note(f"Reddit requested dates: {data['start']} to {data['end']} (UTC). Conversion rates use clicks; view-through purchases are not click-only conversion rates.")
    report.note(f"Reddit CPA / conversion-rate event: {conversion}. The paid overview Conversions column remains attributed purchases.")
    for account in data["accounts"]:
        aid = str(account["account"]["id"])
        ids = {c["id"] for c in account["campaigns"] if selected is None or f"{aid}:{c['id']}" in selected}
        if not ids:
            continue
        rows = [r for r in account["daily"] if r["campaign_id"] in ids]
        metadata = account["account"]["data"]
        report.note(f"Reddit account: {metadata['name']} ({account['account']['resource']}); {metadata['currency']}.")
        report.table("Reddit Ads coverage", [{"Account": account["account"]["resource"], "Data status": state(account),
            "Synced at": metadata["synced_at"], "Attribution": metadata["attribution"], "Timezone": "UTC",
            "Limitations": metadata.get("limitations", []), "Available tracking evidence": metadata.get("known_events", [])}])
        report.table("Reddit Ads dashboard metrics", [{**paid_dashboard_row("Reddit Ads", account_totals(account, rows), "purchases", metadata["currency"]),
                     "Data status": state(account)}])
        report.table("Reddit Ads overview", [account_totals(account, rows, conversion)])
        for level in ("campaigns", "ad_groups", "ads"):
            report.table("Reddit Ads " + level + " performance", performance(account, level, rows, conversion, ids))
            report.table("Reddit Ads " + level + " metadata", scoped_metadata(account, ids)[level])
        report.table("Reddit Ads daily ad performance", [{k: v for k, v in r.items() if k != "metrics"} | r["metrics"] for r in rows])
        report.table("Reddit Ads alternative breakdowns (do not add to totals)",
                     [r for r in account["breakdowns"] if r["campaign_id"] in ids])


def sync_panel(base, mode, role):
    data = call(f"{base}/reddit-ads/status")
    st.subheader("Reddit Ads connection")
    st.caption("Read-only adsread authorization. Credentials and encrypted tokens remain on the backend.")
    if not data["schema_ready"]:
        st.info("Apply 20261119000100_reddit_ads.sql first.")
    if not data["configured"] or not data["authorized"]:
        st.info("Follow REDDIT_ADS_SETUP.md to configure server secrets and run python -m backend.reddit_oauth in a trusted terminal.")
    if st.button("Sync Reddit Ads", disabled=mode != "live" or role != "admin" or not all(data[k] for k in ("configured", "authorized", "schema_ready"))):
        try:
            with st.spinner("Discovering campaigns and importing Reddit daily performance…"):
                result = call(f"{base}/reddit-ads/sync", method="POST", timeout=3600)
            st.success(f"Imported {result['observations']} observations across {result['accounts']} accounts.")
            st.rerun()
        except APIError as exc:
            st.error(str(exc))
    table([{"Account": r["resource"], "Name": r["data"]["name"], "Currency": r["data"]["currency"],
            "Last successful sync": r["data"].get("synced_at")} for r in data["accounts"]])
    table(data["runs"])


def show(base):
    st.title("Reddit Ads")
    today = datetime.now(timezone.utc).date()
    dates = st.date_input("Reddit reporting dates", (today-timedelta(days=30), today-timedelta(days=1)), max_value=today)
    if len(dates) != 2 or not 0 <= (dates[1]-dates[0]).days <= 730:
        return
    try:
        report = load(base, *dates)
    except APIError as exc:
        st.warning(str(exc))
        return
    if not report["accounts"]:
        st.info("Connect and sync Reddit Ads in Connections & Sync.")
        return
    by_id = {a["account"]["id"]: a for a in report["accounts"]}
    selected_account = st.selectbox("Reddit advertising account", list(by_id), format_func=lambda key: by_id[key]["account"]["data"]["name"])
    data = by_id[selected_account]
    currency = data["account"]["data"]["currency"]
    statuses = sorted({c.get("effective_status") or "Unknown" for c in data["campaigns"]})
    statuses = st.multiselect("Campaign status", statuses, default=statuses)
    campaigns = {c["id"]: c for c in data["campaigns"] if (c.get("effective_status") or "Unknown") in statuses}
    selected = st.multiselect("Reddit campaigns", list(campaigns), default=list(campaigns), format_func=lambda key: campaigns[key].get("name") or key)
    rows = [r for r in data["daily"] if r["campaign_id"] in selected]
    st.caption(report["policy"])
    st.caption(f"{currency} · UTC · {state(data)} · Last sync: {data['account']['data']['synced_at']}")
    if state(data) != "Available":
        st.warning("Coverage is incomplete, stale, or contains changed attribution definitions. Check source details before interpreting totals.")
    conversion = st.selectbox("CPA / conversion-rate event", ["purchases", *[f"conversion_{e}_{s}" for e in EVENTS for s in ("clicks", "views")]])
    values = account_totals(data, rows, conversion)
    for column, key in zip(st.columns(4), ("spend", "impressions", "clicks", "purchases")):
        column.metric(key.replace("_", " ").title(), number(values[key], "money" if key == "spend" else "number", currency))
    st.subheader("Campaign performance")
    table(performance(data, "campaigns", rows, conversion, selected))
    st.subheader("Daily ad performance")
    st.caption("Each point is that day's result for one ad. Gaps remain unavailable; these are not cumulative totals.")
    names = {a["id"]: a.get("name") or a["id"] for a in data["ads"]}
    if rows and data["compatible"]:
        frame = pd.DataFrame([{**r["metrics"], "day": r["day"], "ad": f"{names.get(r['ad_id'],r['ad_id'])} · {r['ad_id']}"} for r in rows])
        frame["day"] = pd.to_datetime(frame["day"])
        pieces = []
        for ad, part in frame.groupby("ad"):
            part = part.set_index("day").reindex(pd.date_range(dates[0], dates[1]))
            part["ad"] = ad
            pieces.append(part.rename_axis("day").reset_index())
        frame = pd.concat(pieces)
        columns = st.columns(2)
        for i, key in enumerate(("spend", "impressions", "clicks", "purchases", "purchase_value")):
            if frame[key].notna().any():
                columns[i % 2].plotly_chart(px.line(frame, x="day", y=key, color="ad", markers=True,
                    labels={key: key.replace("_", " ").title() + (f" ({currency})" if key in ("spend", "purchase_value") else ""), "day": "Date"}), use_container_width=True)
            else:
                columns[i % 2].info(f"{key.replace('_',' ').title()} unavailable.")
    with st.expander("Ad groups, ads, conversion and video metrics"):
        table(performance(data, "ad_groups", rows, conversion, selected))
        table(performance(data, "ads", rows, conversion, selected))
    from frontend.reports import Report
    export = Report()
    add_report(export, report, [f"{selected_account}:{key}" for key in selected], conversion)
    with st.expander("Source metadata, targeting and attribution"):
        table([{**data["account"]["data"], "coverage_days": "See selected-period coverage status"}])
        table([c for c in data["campaigns"] if c["id"] in selected])
        groups = [g for g in data["ad_groups"] if g["campaign_id"] in selected]
        table(groups)
        table([a for a in data["ads"] if a.get("ad_group_id") in {g["id"] for g in groups}])
    blob, extension, mime = export.export()
    st.download_button("Download Reddit Ads report", blob, file_name=f"reddit-ads-{dates[0]}-{dates[1]}.{extension}", mime=mime)
