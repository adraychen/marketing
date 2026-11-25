from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from backend.metrics import previous_period, ratio
from backend.meta_purchase_metrics import PURCHASE_NOTE, purchase_totals
from frontend.client import APIError, call
from frontend.views import number, table
from backend.meta_platforms import NOTE as PLATFORM_NOTE, METRICS as PLATFORM_METRICS, reconcile


def load_report(base,start,end):
    return call(f"{base}/meta-ads/report",params={"start":str(start),"end":str(end)})


def campaign_option_label(row):
    return f"{row.get('name') or row['id']} · {row.get('effective_status') or row.get('status') or 'Unknown status'} · {row['id']}"


def active_campaigns(data):
    return [row for row in data['campaigns']
            if (row.get('effective_status') or row.get('status')) == 'ACTIVE']


def scoped(data, campaign_ids):
    ids = set(campaign_ids)
    daily = [row for row in data["daily"] if row["campaign_id"] in ids]
    platforms = [row for row in data.get("platform_daily", []) if row["campaign_id"] in ids]
    return {**data, "platform_daily": platforms, "platform_reconciliation": reconcile(daily, platforms),
            "campaigns":[row for row in data["campaigns"] if row["id"] in ids],
            "adsets":[row for row in data["adsets"] if row.get("campaign_id") in ids],
            "ads":[row for row in data["ads"] if row.get("campaign_id") in ids],
            "daily":[row for row in data["daily"] if row["campaign_id"] in ids]}


def platform_coverage(data):
    state = data.get("platform_status", {})
    return [{"channel": "Meta Paid — " + platform.title(), "data_source": "Meta Ads publisher_platform",
             "state": state.get("state", "unavailable") if data.get("account") else "disconnected",
             "complete_date_coverage": data.get("platform_coverage", False), "last_successful_sync": state.get("last_successful_sync"),
             "available_metrics": [key for key in (*PLATFORM_METRICS, "reach") if any(r.get(key) is not None for r in data.get("platform_daily", []) if r["publisher_platform"] == platform)],
             "unavailable_metrics": [key for key in (*PLATFORM_METRICS, "reach") if not any(r.get(key) is not None for r in data.get("platform_daily", []) if r["publisher_platform"] == platform)],
             "attribution_limitations": PLATFORM_NOTE, "tracking_or_permission_errors": state.get("error")}
            for platform in ("facebook", "instagram")]


def totals(data):
    rows = data["daily"]
    result = {}
    for key in ("spend","impressions","clicks","inline_link_clicks"):
        values = [row.get(key) for row in rows]
        result[key] = (sum(values) if values and all(value is not None for value in values)
                       else 0 if not rows and data["coverage"] and key in ("spend","impressions","clicks","inline_link_clicks") else None)
    result["ctr"] = ratio(result["clicks"],result["impressions"])
    result["cpc"] = ratio(result["spend"],result["clicks"])
    result.update(purchase_totals(rows, result['spend'], data['coverage']))
    return result


def campaigns(data):
    grouped = defaultdict(list)
    for row in data["daily"]:
        grouped[row["campaign_id"]].append(row)
    return [{"campaign_id":row["id"],"campaign":row.get("name"),"status":row.get("effective_status"),
             **totals({**data,"daily":grouped[row["id"]]})} for row in data["campaigns"]]


def platform_scope(data, platform):
    if platform is None:
        return data
    state = data.get('platform_status', {})
    return {**data, 'daily': [r for r in data.get('platform_daily', []) if r['publisher_platform'] == platform],
            'coverage': bool(data.get('platform_coverage')),
            'stale': data.get('stale', False) or state.get('state') != 'available'}


def platform_summary(data):
    labels = {'facebook': 'Facebook Ads', 'instagram': 'Instagram Ads'}
    platforms = list(labels) + sorted({r['publisher_platform'] for r in data.get('platform_daily', [])} - labels.keys())
    account = data.get('account') or {}
    result = []
    for platform in platforms:
        scoped_data = platform_scope(data, platform)
        values = totals(scoped_data)
        result.append({'Platform': labels.get(platform, 'Meta · ' + platform.replace('_', ' ').title()),
                       'Account': account.get('data', {}).get('name'),
                       'Currency': account.get('data', {}).get('currency'),
                       'Status': ('Breakdown unavailable — sync Meta Paid Ads' if not data.get('platform_status', {}).get('last_successful_sync')
                                  else 'Stale import' if scoped_data['stale'] else 'Imported' if scoped_data['coverage'] else 'Partial coverage'),
                       'Spend': values['spend'], 'Impressions': values['impressions'], 'Clicks': values['clicks'],
                       'Link clicks': values['inline_link_clicks'],
                       'Link CTR': ratio(values['inline_link_clicks'], values['impressions']),
                       'Cost per link click': ratio(values['spend'], values['inline_link_clicks']),
                       'Meta website purchases': values['website_purchases'],
                       'CTR': values['ctr'], 'Avg. CPC': values['cpc'],
                       'Purchase value': values['website_purchase_value'], 'ROAS': values['website_roas']})
    return result


def add_meta_report(report,base,start,end,campaign_ids,data=None,include_ai=False):
    data = scoped(data or load_report(base,start,end),campaign_ids)
    account = data["account"]
    report.note(data["policy"])
    report.note(PURCHASE_NOTE)
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
    from frontend.reports import paid_dashboard_row
    report.table("Meta Ads dashboard metrics", [{**paid_dashboard_row("Meta Ads", totals(data), "website_purchases",
        account["data"].get("currency") if account else None),
        "Data status": "Needs refresh" if data["stale"] else "Available" if data["coverage"] else "Partial period"}])
    report.table("Meta paid campaign performance",campaigns(data))
    report.table("Meta paid campaign settings",data["campaigns"])
    report.table("Meta paid ad set settings and targeting",data["adsets"])
    report.table("Meta paid ads and creative",data["ads"])
    report.table("Meta paid daily ad performance",data["daily"])
    report.note(PLATFORM_NOTE)
    report.table("Meta publisher-platform channel coverage", platform_coverage(data))
    report.table("Meta Facebook and Instagram summary", platform_summary(data))
    report.table("Meta publisher-platform daily ad performance", data.get("platform_daily", []))
    report.table("Meta publisher-platform reconciliation", data.get("platform_reconciliation", []))
    from frontend.campaign_evaluation import add_report, load
    try:
        add_report(report, load(base, start, end), campaign_ids, base, include_ai)
    except APIError as exc:
        report.note('Campaign evaluation unavailable: ' + str(exc))


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
    available = active_campaigns(data)
    if not available:
        st.info("No active Meta campaigns were found in the latest import. Sync Meta Paid Ads to refresh campaign status.")
        return
    st.caption("Showing active campaigns only, based on the latest sync.")
    selected=st.multiselect("Meta campaigns",[row["id"] for row in available],
                            default=[row["id"] for row in available],
                            key=f"meta-active-campaigns:{base}",
                            format_func=lambda identifier:next((campaign_option_label(row) for row in available if row["id"]==identifier),identifier))
    data=scoped(data,selected)
    prior_start,prior_end=previous_period(start,end)
    previous=scoped(load_report(base,prior_start,prior_end),selected)
    st.subheader('Facebook and Instagram performance')
    table(platform_summary(data), download_name='meta-platform-performance.csv')
    platforms = [None, 'facebook', 'instagram'] + sorted({r['publisher_platform'] for r in data.get('platform_daily', [])} - {'facebook', 'instagram'})
    platform = st.selectbox('Delivery platform', platforms,
                            format_func=lambda value: 'All Meta placements' if value is None else value.replace('_', ' ').title(),
                            key=f'meta-delivery-platform:{base}')
    data, previous = platform_scope(data, platform), platform_scope(previous, platform)
    if platform is not None:
        st.caption('Cards, campaign performance and daily charts follow the selected delivery platform. Campaign settings are shared across placements.')
        if not data.get('platform_status', {}).get('last_successful_sync'):
            st.info('Platform breakdown unavailable. Apply the existing Meta platform delivery migration and sync Meta Paid Ads.')
    current_totals,prior_totals=totals(data),totals(previous)
    currency=account["data"]["currency"]
    st.caption(f"{currency} / {account['data']['timezone_name']} / Previous period {prior_start} to {prior_end}")
    if not data["coverage"]:
        st.warning("Partial or missing date coverage. Figures below describe reported rows only.")
    if data["stale"]:
        st.warning("Last successful Meta paid import is over 48 hours old.")
    if any(current_totals[key + '_status'] == 'Reported subtotal' for key in ('website_purchases', 'website_purchase_value')):
        st.caption(PURCHASE_NOTE)
    if status.get("runs") and status["runs"][0]["status"]=="failed":
        st.warning("Latest paid sync failed. Showing the last successful import.")
    for columns,keys in ((st.columns(3),[("spend","Spend","money"),("clicks","All clicks","count"),("inline_link_clicks","Link clicks","count")]),
                         (st.columns(3),[("website_purchases","Meta website purchases","count"),("website_purchase_value","Meta website purchase value","money"),("website_roas","Meta website ROAS","multiple")])):
        for column,(key,label,kind) in zip(columns,keys):
            now,old=current_totals[key],prior_totals[key]
            purchase_key = 'website_purchase_value' if key == 'website_roas' else key
            comparable = (purchase_key not in ('website_purchases', 'website_purchase_value') or
                          all(t[purchase_key + '_status'] == 'Reported total' for t in (current_totals, prior_totals)))
            delta=number(now-old,kind,currency) if comparable and data["coverage"] and previous["coverage"] and now is not None and old is not None else None
            column.metric(label,number(now,kind,currency) if now is not None else "Not available",delta)
    with st.expander("Definitions and coverage"):
        st.write(data["policy"])
        st.write(PURCHASE_NOTE)
        table([{"synced_from":account["synced_from"],"synced_through":account["synced_through"],"synced_at":account["synced_at"]}])
    daily,performance,settings,creative=st.tabs(["Daily performance","Campaign performance","Campaigns & ad sets","Ads & creative"])
    with performance:
        table(campaigns(data))
        st.markdown("**Facebook and Instagram delivery**")
        st.caption('The coverage and raw delivery tables below include all placements for the selected campaigns.')
        st.caption(PLATFORM_NOTE)
        table(platform_coverage(data))
        table(data.get("platform_daily", []))
        if data.get("platform_reconciliation"):
            st.warning("Platform breakdown and unbroken delivery have gaps or disagreements. See reconciliation; totals have not been replaced.")
            table(data["platform_reconciliation"])
        if platform is None:
            from frontend.campaign_evaluation import show
            show(base, start, end, selected)
        else:
            st.caption('Cross-source campaign evaluation is available under All Meta placements; Shopify and GA4 outcomes are not split by Meta delivery platform.')
    with settings:
        st.caption("Budget and bid fields below are raw Meta minor currency units. Settings reflect latest discovery.")
        table(data["campaigns"])
        table(data["adsets"])
    with creative:
        table(data["ads"])
    with daily:
        from frontend.meta_daily_charts import show_daily_charts
        show_daily_charts(data, start, end, currency)
