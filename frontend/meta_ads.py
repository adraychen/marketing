from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from backend.metrics import previous_period, ratio
from frontend.client import APIError, call
from frontend.views import number, table


def load_report(base,start,end):
    return call(f"{base}/meta-ads/report",params={"start":str(start),"end":str(end)})


def scoped(data, campaign_ids):
    ids = set(campaign_ids)
    return {**data, "campaigns":[row for row in data["campaigns"] if row["id"] in ids],
            "adsets":[row for row in data["adsets"] if row.get("campaign_id") in ids],
            "ads":[row for row in data["ads"] if row.get("campaign_id") in ids],
            "daily":[row for row in data["daily"] if row["campaign_id"] in ids]}


def totals(data):
    rows = data["daily"]
    result = {}
    for key in ("spend","impressions","clicks","inline_link_clicks","website_purchases","website_purchase_value"):
        values = [row.get(key) for row in rows]
        result[key] = (sum(values) if values and all(value is not None for value in values)
                       else 0 if not rows and data["coverage"] and key in ("spend","impressions","clicks","inline_link_clicks") else None)
    result["ctr"] = ratio(result["clicks"],result["impressions"])
    result["cpc"] = ratio(result["spend"],result["clicks"])
    result["website_roas"] = ratio(result["website_purchase_value"],result["spend"])
    return result


def campaigns(data):
    grouped = defaultdict(list)
    for row in data["daily"]:
        grouped[row["campaign_id"]].append(row)
    return [{"campaign_id":row["id"],"campaign":row.get("name"),"status":row.get("effective_status"),
             **totals({**data,"daily":grouped[row["id"]]})} for row in data["campaigns"]]


def add_meta_report(report,base,start,end,campaign_ids,data=None):
    data = scoped(data or load_report(base,start,end),campaign_ids)
    account = data["account"]
    report.note(data["policy"])
    report.table("Meta paid coverage", [{"account_id":account["account_id"] if account else None,
        "start":str(start),"end":str(end),"complete":data["coverage"],"stale":data["stale"],
        "synced_from":account["synced_from"] if account else None,
        "synced_through":account["synced_through"] if account else None,
        "metadata":account["data"] if account else None}])
    if not data["coverage"]:
        report.note("Meta paid import does not fully cover these dates. Reported rows are partial, not complete totals.")
    if data["stale"]:
        report.note("Meta paid import is over 48 hours old.")
    report.table("Meta paid overview",[totals(data)])
    report.table("Meta paid campaign performance",campaigns(data))
    report.table("Meta paid campaign settings",data["campaigns"])
    report.table("Meta paid ad set settings and targeting",data["adsets"])
    report.table("Meta paid ads and creative",data["ads"])
    report.table("Meta paid daily ad performance",data["daily"])


def sync_panel(base,mode,role):
    status = call(f"{base}/meta-ads/status")
    if not status["configured"]:
        st.info("Meta paid ads are not connected. Add the paid-ads settings to Render, separate from the organic Page token.")
    else:
        st.write(f"Configured paid ad account: {status['account_id']}")
        st.caption("Configuration is present. A successful sync verifies access.")
    if status.get("schema_ready") is False:
        st.warning("Apply the Meta paid-ads migration before importing.")
    st.write("Token expiry: " + (status.get("token_expires_at") or "Unknown"))
    st.caption(status.get("expiry_source", ""))
    if status.get("token_expiring"):
        st.warning("The configured token expiry is within seven days or has passed. Renew it in Render.")
    if mode == "live" and role == "admin" and status["configured"]:
        if st.button("Sync Meta paid ads",type="primary",disabled=status.get("schema_ready") is False):
            try:
                with st.spinner("Importing Meta paid performance..."):
                    result = call(f"{base}/meta-ads/sync","POST",timeout=600)
                st.success("Meta paid synchronization completed.")
                table([{"Data":key,"Records":value} for key,value in result["counts"].items()])
                status = call(f"{base}/meta-ads/status")
            except APIError as exc:
                st.error(str(exc))
    st.caption("Imports campaigns, ad sets, ads/creative, and daily paid delivery/actions. "
               "Existing-post or dynamic creative may expose only partial text. Reach is not added across ads or dates.")
    table(status["accounts"])
    with st.expander("Paid sync history",expanded=True):
        table(status["runs"])
    return status


def show_meta_ads(base,mode):
    st.title("Meta Ads")
    st.caption("Paid Facebook and Instagram placements from the configured Meta ad account. Organic Page analytics remain separate.")
    status = call(f"{base}/meta-ads/status")
    if not status["accounts"]:
        st.info("Meta paid reporting is not connected or has no successful import yet. Open Connections & Sync > Meta Paid Ads.")
        return
    account = next((row for row in status["accounts"] if row["account_id"]==status.get("account_id")),status["accounts"][0])
    last = datetime.now(ZoneInfo(account["data"]["timezone_name"])).date()-timedelta(days=1)
    dates = st.date_input("Meta reporting dates",(last-timedelta(days=29),last),max_value=last,key=f"meta-dates:{base}")
    if len(dates)!=2:
        return
    start,end=dates
    if start>end or (end-start).days>365:
        st.info("Choose a valid period of up to 366 days.")
        return
    data=load_report(base,start,end)
    selected=st.multiselect("Meta campaigns",[row["id"] for row in data["campaigns"]],
                            default=[row["id"] for row in data["campaigns"]],
                            format_func=lambda identifier:next((row.get("name") or identifier for row in data["campaigns"] if row["id"]==identifier),identifier))
    data=scoped(data,selected)
    prior_start,prior_end=previous_period(start,end)
    previous=scoped(load_report(base,prior_start,prior_end),selected)
    current_totals,prior_totals=totals(data),totals(previous)
    currency=account["data"]["currency"]
    st.caption(f"{currency} / {account['data']['timezone_name']} / Previous period {prior_start} to {prior_end}")
    if not data["coverage"]:
        st.warning("Partial or missing date coverage. Figures below describe reported rows only.")
    if data["stale"]:
        st.warning("Last successful Meta paid import is over 48 hours old.")
    if status.get("runs") and status["runs"][0]["status"]=="failed":
        st.warning("Latest paid sync failed. Showing the last successful import.")
    for columns,keys in ((st.columns(3),[("spend","Spend","money"),("clicks","All clicks","count"),("inline_link_clicks","Link clicks","count")]),
                         (st.columns(3),[("website_purchases","Meta website purchases","count"),("website_purchase_value","Meta website purchase value","money"),("website_roas","Meta website ROAS","multiple")])):
        for column,(key,label,kind) in zip(columns,keys):
            now,old=current_totals[key],prior_totals[key]
            delta=number(now-old,kind,currency) if data["coverage"] and previous["coverage"] and now is not None and old is not None else None
            column.metric(label,number(now,kind,currency) if now is not None else "Not available",delta)
    with st.expander("Definitions and coverage"):
        st.write(data["policy"])
        table([{"synced_from":account["synced_from"],"synced_through":account["synced_through"],"synced_at":account["synced_at"]}])
    performance,settings,creative,daily=st.tabs(["Campaign performance","Campaigns & ad sets","Ads & creative","Daily metrics"])
    with performance:
        table(campaigns(data))
    with settings:
        st.caption("Budget and bid fields below are raw Meta minor currency units. Settings reflect latest discovery.")
        table(data["campaigns"])
        table(data["adsets"])
    with creative:
        table(data["ads"])
    with daily:
        table(data["daily"])
