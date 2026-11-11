import difflib
from collections import defaultdict
from datetime import date
from urllib.parse import urlsplit

import pandas as pd
import plotly.express as px
import streamlit as st

from backend.metrics import previous_period, summarize
from backend.models import Creative
from backend.validation import validate
from frontend.client import APIError, call

METRICS = (
    ("spend", "Spend", "money"), ("value", "Conversion value", "money"),
    ("roas", "ROAS", "multiple"), ("conversions", "Conversions", "decimal"),
    ("cpa", "Cost / conversion", "money"), ("impressions", "Impressions", "count"),
    ("clicks", "Clicks", "count"), ("ctr", "CTR", "percent"),
    ("cpc", "Avg. CPC", "money"), ("conversion_rate", "Conversion rate", "percent"),
)


def number(value, kind, currency):
    if value is None:
        return "—"
    if kind == "money":
        return f"{currency} {value:,.2f}"
    if kind == "percent":
        return f"{value:.2%}"
    if kind == "multiple":
        return f"{value:.2f}×"
    if kind == "count":
        return f"{value:,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def period_rows(report, kind, start, end):
    return [row for row in report["performance"] if row["kind"] == kind
            and start <= date.fromisoformat(row["day"]) <= end]


def coverage(account, start, end):
    return bool(account["synced_from"] and account["synced_through"]
                and account["synced_from"] <= start.isoformat()
                and end.isoformat() <= account["synced_through"])


def dashboard(report, account, _route, start, end):
    st.title("Performance overview")
    st.caption("Google Ads performance · Conversion value and ROAS use Google attribution.")
    currency = account["currency"]
    before_start, before_end = previous_period(start, end)
    current = period_rows(report, "campaigns", start, end)
    prior = period_rows(report, "campaigns", before_start, before_end)
    compare = coverage(account, before_start, end)
    complete = coverage(account, start, end)
    if not complete:
        st.warning("The selected period extends beyond imported dates. Totals reflect available data.")
    elif not compare:
        st.caption("The full previous period is outside imported coverage.")
    if not current:
        st.info("No campaign activity was returned for these dates.")
    totals, previous = summarize(current, currency), summarize(prior, currency)
    if compare:
        st.caption(f"Comparing {start:%b %d, %Y} – {end:%b %d, %Y} with "
                   f"{before_start:%b %d, %Y} – {before_end:%b %d, %Y}.")
    for offset in (0, 5):
        for column, (key, label, kind) in zip(st.columns(5), METRICS[offset:offset + 5]):
            value, old = totals[key], previous[key]
            change = number(value - old, kind, currency) if compare and value is not None and old is not None else None
            if change and kind == "percent":
                change = f"{(value - old) * 100:+.2f} pp"
            elif change:
                change = f"+{change}" if value > old else (f"−{number(old - value, kind, currency)}" if value < old else change)
            if change and value == old:
                change = None
            column.metric(label, number(value, kind, currency), change, border=True,
                          delta_color="inverse" if key in {"cpa", "cpc"} else "normal")
    st.caption("Conversion rate uses ad interactions. A dash indicates missing data or an undefined ratio.")
    if not current:
        return

    st.subheader("Performance over time")
    chart_metrics = {label: key for key, label, _ in METRICS}
    selected = st.selectbox("Chart metric", chart_metrics)
    metric = chart_metrics[selected]
    points = []
    periods = [("Selected period", start, current)]
    if compare:
        periods.append(("Previous period", before_start, prior))
    for label, first, rows in periods:
        daily = defaultdict(list)
        for row in rows:
            daily[date.fromisoformat(row["day"])].append(row)
        for day in sorted(daily):
            points.append({"Period": label, "Day in period": (day - first).days + 1, "Date": day,
                           selected: summarize(daily[day], currency)[metric]})
    figure = px.line(pd.DataFrame(points), x="Day in period", y=selected, color="Period",
                     markers=True, hover_data=["Date"], color_discrete_sequence=["#007f73", "#8c79d4"])
    figure.update_layout(height=380, legend_title_text="")
    if metric in {"ctr", "conversion_rate"}:
        figure.update_yaxes(tickformat=".1%")
    st.plotly_chart(figure, width="stretch")


def pretty(value):
    return str(value or "—").replace("_", " ").title()


def table(rows):
    if not rows:
        st.info("No rows match this view.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def metric_by_resource(report, kind, start, end, currency):
    items = defaultdict(list)
    for row in period_rows(report, kind, start, end):
        items[row["resource"]].append(row)
    return {key: summarize(rows, currency) for key, rows in items.items()}


def search(items, fields, label):
    query = st.text_input(label, placeholder="Search…").strip().casefold()
    return [item for item in items if not query or any(
        query in str(item["data"].get(field) or "").casefold() for field in fields)]


def select_item(label, items, text, key=None):
    if not items:
        return None
    lookup = {item["resource"]: item for item in items}
    resource = st.selectbox(label, list(lookup), format_func=lambda key: text(lookup[key]), key=key)
    return lookup[resource]


def campaign_label(item):
    return item["data"]["name"]


def ad_label(item):
    ad = item["data"]
    headlines = (ad.get("creative") or {}).get("headlines", [])
    title = ad.get("name") or (headlines[0]["text"] if headlines else pretty(ad["type"]))
    return f"{title} · {ad['id']}"


def show_ad(ad, group=None, campaign=None):
    data = ad["data"]
    st.subheader(ad_label(ad))
    st.caption("  ·  ".join(value for value in (
        f"Campaign: {campaign}" if campaign else None, f"Ad group: {group}" if group else None,
        pretty(data["type"]), pretty(data["status"]),
        f"Strength: {pretty(data['strength'])}" if data.get("strength") else None) if value))
    creative = data.get("creative")
    if creative is not None:
        with st.container(border=True):
            st.caption("Illustrative preview · Google may serve different combinations.")
            urls = creative["final_urls"]
            try:
                host = urlsplit(urls[0]).netloc if urls else ""
            except ValueError:
                host = ""
            st.text("/".join(filter(None, [host, creative["path1"], creative["path2"]])))
            for key, prefix, count in (("headlines", "HEADLINE", 3), ("descriptions", "DESCRIPTION", 2)):
                pool = iter(asset["text"] for asset in creative[key] if not asset["pin"])
                pieces = []
                for position in range(1, count + 1):
                    pinned = next((asset["text"] for asset in creative[key]
                                   if asset["pin"] == f"{prefix}_{position}"), None)
                    pieces.append(pinned or next(pool, ""))
                st.text((" | " if key == "headlines" else " ").join(filter(None, pieces)))
        for kind in ("headlines", "descriptions"):
            st.markdown(f"**{kind.title()} ({len(creative[kind])})**")
            table([{"Order": i, "Asset": asset["text"], "Pin": pretty(asset["pin"])}
                   for i, asset in enumerate(creative[kind], 1)])
        st.text(f"Display paths: {creative['path1'] or '—'} / {creative['path2'] or '—'}")
        for url in creative["final_urls"]:
            st.text(url)
    elif data.get("read_only"):
        for key, values in data["read_only"].items():
            st.markdown(f"**{key.title()}**")
            for value in values:
                st.text(value)
        for url in data.get("final_urls", []):
            st.text(url)
    else:
        st.info("A creative preview is unavailable for this ad format.")
        st.text(ad["resource"])
        for url in data.get("final_urls", []):
            st.text(url)


def drill(report, campaign):
    group = select_item("Ad group", [item for item in report["ad_groups"] if item["parent"] == campaign["resource"]],
                        lambda item: f"{item['data']['name']} · {pretty(item['data']['status'])}")
    if not group:
        st.info("No ad groups were imported for this campaign.")
        return
    ad = select_item("Advertisement", [item for item in report["ads"] if item["parent"] == group["resource"]], ad_label)
    if ad:
        show_ad(ad, group["data"]["name"], campaign_label(campaign))
    else:
        st.info("No ads were imported for this ad group.")


def campaigns(report, account, _route, start, end):
    st.title("Campaigns")
    st.caption("Filter campaigns in the selected account, then open a campaign to inspect its ads.")
    if not coverage(account, start, end):
        st.caption("Performance reflects available imported dates.")
    items = report["campaigns"]
    statuses = st.multiselect("Status", sorted({item["data"]["status"] or "UNKNOWN" for item in items}), format_func=pretty)
    types = st.multiselect("Campaign type", sorted({item["data"]["type"] or "UNKNOWN" for item in items}), format_func=pretty)
    items = [item for item in search(items, ("name", "id"), "Search campaigns")
             if (not statuses or (item["data"]["status"] or "UNKNOWN") in statuses)
             and (not types or (item["data"]["type"] or "UNKNOWN") in types)]
    currency = account["currency"]
    totals = metric_by_resource(report, "campaigns", start, end, currency)
    empty = summarize([], currency)
    table([{
        "Campaign": item["data"]["name"], "ID": item["data"]["id"], "Platform": account["platform"],
        "Type": pretty(item["data"]["type"]), "Status": pretty(item["data"]["status"]),
        "Daily budget": item["data"]["daily_budget"], "Spend": round(m["spend"], 2),
        "Impressions": m["impressions"], "Clicks": m["clicks"], "Conversions": m["conversions"],
        "Conversion value": m["value"], "ROAS": m["roas"],
    } for item in items for m in [totals.get(item["resource"], empty)]])
    campaign = select_item("Open campaign", items, campaign_label)
    if campaign:
        drill(report, campaign)


def content(report, _account, _route, _start, _end):
    st.title("Ad Content")
    st.caption("Browse imported creative and inspect every responsive search ad asset in order.")
    campaign = select_item("Campaign", report["campaigns"], campaign_label)
    if campaign:
        drill(report, campaign)
    else:
        st.info("No campaigns have been imported.")


def keywords(report, account, _route, start, end):
    st.title("Keywords & search terms")
    view = st.segmented_control("View", ["Configured keywords", "Actual search terms"], default="Configured keywords")
    if view is None:
        return
    kind = "keywords" if view == "Configured keywords" else "search_terms"
    st.caption("Keywords are configured in the ad account." if kind == "keywords" else
               "Search terms are queries Google reported. Privacy thresholds apply and Performance Max terms are excluded.")
    if not coverage(account, start, end):
        st.caption("Performance reflects available imported dates.")
    campaign = select_item("Filter to campaign", [{"resource": "*", "data": {"name": "All campaigns"}}]
                           + report["campaigns"], campaign_label)
    groups = {item["resource"]: item for item in report["ad_groups"]}
    campaign_names = {item["resource"]: campaign_label(item) for item in report["campaigns"]}
    currency = account["currency"]
    totals = metric_by_resource(report, kind, start, end, currency)
    empty = summarize([], currency)
    rows = []
    for item in search(report[kind], ("text",), "Search keywords" if kind == "keywords" else "Search actual queries"):
        if kind == "search_terms" and item["resource"] not in totals:
            continue
        group = groups.get(item["parent"], {})
        campaign_id = group.get("parent")
        if campaign["resource"] != "*" and campaign["resource"] != campaign_id:
            continue
        m = totals.get(item["resource"], empty)
        row = {"Keyword" if kind == "keywords" else "Search term": item["data"]["text"]}
        if kind == "keywords":
            row["Match type"] = pretty(item["data"]["match_type"])
        row.update({
            "Campaign": campaign_names.get(campaign_id), "Ad group": group.get("data", {}).get("name"),
            "Status": pretty(item["data"]["status"]), "Impressions": m["impressions"], "Clicks": m["clicks"],
            "Spend": round(m["spend"], 2), "CTR (%)": round(m["ctr"] * 100, 2) if m["ctr"] is not None else None,
            "Avg. CPC": m["cpc"], "Conversions": m["conversions"], "Conversion value": m["value"], "ROAS": m["roas"],
        })
        rows.append(row)
    table(rows)


def edit_assets(kind, creative, token):
    prefix, count = ("HEADLINE", 3) if kind == "headlines" else ("DESCRIPTION", 2)
    st.markdown(f"**{kind.title()}**")
    pins = [""] + [f"{prefix}_{position}" for position in range(1, count + 1)]
    original = creative[kind]
    pins += sorted({asset["pin"] for asset in original} - set(pins))
    edited = st.data_editor(pd.DataFrame(original, columns=["text", "pin"]), hide_index=True,
                            num_rows="dynamic", width="stretch", key=f"{token}:{kind}", column_config={
                                "text": st.column_config.TextColumn("Asset", required=True, width="large"),
                                "pin": st.column_config.SelectboxColumn("Pin to", options=pins),
                            })
    assets = edited.fillna("").to_dict("records")
    return [asset for asset in assets if asset["text"].strip() or asset["pin"]]


def comparison(original, revised):
    def lines(creative):
        output = []
        for kind in ("headlines", "descriptions"):
            output.append(kind.title())
            for index, asset in enumerate(creative[kind], 1):
                pin = f" [Pinned to {pretty(asset['pin'])}]" if asset["pin"] else ""
                output.append(f"  {index}. {asset['text']}{pin}")
        output.extend(["", "Final URLs", *[f"  {url}" for url in creative["final_urls"]], "",
                       f"Display path 1: {creative['path1'] or '—'}",
                       f"Display path 2: {creative['path2'] or '—'}"])
        return output

    delta = list(difflib.unified_diff(lines(original), lines(revised),
                                      fromfile="Current import", tofile="Draft", lineterm=""))
    if delta:
        st.code("\n".join(delta), language="diff")
    else:
        st.caption("The draft matches the current imported creative.")


def drafts(report, account, route, _start, _end):
    st.title("Draft revisions")
    st.caption("Edit a responsive search ad and compare your revision with its current import.")
    saved = {item["resource"]: item for item in call(f"{route}/drafts")}
    st.caption(f"{len(saved)} saved draft(s) in {account['name']}")
    ads = [ad for ad in report["ads"] if ad["data"].get("creative") is not None]
    ads.sort(key=lambda item: item["resource"] not in saved)
    ad = select_item("Responsive search ad", ads,
                     lambda item: ad_label(item) + (" · Saved draft" if item["resource"] in saved else ""))
    if not ad:
        st.info("Import responsive search ads to create drafts.")
        return
    resource = ad["resource"]
    original = ad["data"]["creative"]
    draft = saved.get(resource)
    version = draft["version"] if draft else None
    initial = draft["creative"] if draft else original
    token = f"{route}:{resource}:{version}:{st.session_state.get('draft_epoch', 0)}"
    headlines = edit_assets("headlines", initial, token)
    descriptions = edit_assets("descriptions", initial, token)
    urls = st.text_area("Final URLs (one per line)", "\n".join(initial["final_urls"]), key=f"{token}:urls")
    left, right = st.columns(2)
    path1 = left.text_input("Display path 1", initial["path1"], key=f"{token}:path1")
    path2 = right.text_input("Display path 2", initial["path2"], key=f"{token}:path2")
    proposed = dict(headlines=headlines, descriptions=descriptions, path1=path1, path2=path2,
                    final_urls=[url.strip() for url in urls.splitlines() if url.strip()])
    try:
        issues = validate(Creative.model_validate(proposed))
    except ValueError:
        issues = ["Reduce the number or length of the assets and URLs to the supported limits."]
    with st.expander("Compare with current imported creative"):
        comparison(original, proposed)
    st.subheader("Validation")
    if issues:
        for issue in issues:
            st.warning(issue)
    else:
        st.caption("The revision passes local responsive search ad checks.")
    if st.button("Save draft", disabled=bool(issues), type="primary"):
        call(f"{route}/drafts", "PUT", params={"resource": resource},
             payload={"creative": proposed, "version": version})
        st.session_state.flash = "Draft saved."
        st.rerun()
    if draft and st.checkbox("Confirm discard of saved draft", key=f"{token}:confirm"):
        if st.button("Discard draft"):
            call(f"{route}/drafts", "DELETE", params={"resource": resource, "version": version})
            st.session_state.draft_epoch = st.session_state.get("draft_epoch", 0) + 1
            st.session_state.flash = "Draft discarded."
            st.rerun()


def select_ad_account(org_id, base):
    accounts = {row["id"]: row for row in call(f"{base}/accounts")}
    if not accounts:
        return None
    selected = st.selectbox("Ad account", list(accounts),
                            format_func=lambda key: f"{accounts[key]['name']} · {accounts[key]['currency']}",
                            key=f"account:{org_id}")
    return accounts[selected]


CONTENT_SYNC_SOURCES = {
    "Facebook": ("facebook", {"facebook": "Published posts", "facebook_insights": "Post insights",
                              "facebook_page_insights": "Page insights"}),
    "Shopify": ("shopify", {"shopify": "Published articles", "ga4": "GA4 article performance",
                           "ga4_traffic": "GA4 traffic sources", "search_console": "Search performance",
                           "search_console_inspection": "Search indexing",
                           "shopify_orders": "Shopify orders and UTM attribution", "ga4_store": "Storewide GA4 sessions"}),
}


def sync_result(status, records, message=None):
    if status == "succeeded":
        if not records:
            return "No data returned"
        return "Synced with warnings" if message else "Synced"
    if status in {"failed", "partial"} and "permission unavailable" in (message or "").lower():
        return "Permission needed" if status == "failed" else "Partial · permission needed"
    return pretty(status) if status else "No run recorded"


def content_sync(base, platform):
    source, labels = CONTENT_SYNC_SOURCES[platform]
    catalog = call(f"{base}/content")
    accounts = sorted({item["source_account"] for item in catalog["items"]
                       if item["source"] == source and item["source_account"] and item["active"]})
    if accounts:
        selected = st.selectbox("Facebook Page ID" if source == "facebook" else "Shopify store", accounts,
                                key=f"sync-source:{base}:{source}")
        total = sum(item["source"] == source and item["source_account"] == selected and item["active"]
                    for item in catalog["items"])
        st.caption(f"{total:,} published items in the dashboard for this selection.")
    else:
        st.info("No account with imported content yet. Check the content sync job configuration and latest run.")
    st.caption("The content sync job refreshes the Page and store configured on Render. "
               "Run the job there to refresh them on demand.")
    status = {row["source"]: row for row in catalog["sources"]}
    st.markdown("**Latest organization sync by source**")
    st.caption("The job records these results for the organization as a whole.")
    rows = []
    for key, label in labels.items():
        row = status.get(key, {})
        rows.append({"Data": label, "Result": sync_result(row.get("status"), row.get("records"), row.get("message")),
                     "Records": row.get("records"), "Last run": row.get("finished_at"),
                     "Last success": row.get("last_success_at"),
                     "Details": row.get("message")})
    table(rows)


def settings(mode, org, base):
    st.title("Connections & Sync")
    st.caption(f"{org['name']} · {pretty(org['role'])}")
    platform = st.radio("Platform", ("Google Ads", "Facebook", "Meta Paid Ads", "Shopify", "TikTok Ads"),
                        horizontal=True, key=f"sync-platform:{base}")
    if platform == "Meta Paid Ads":
        from frontend.meta_ads import sync_panel
        sync_panel(base,mode,org["role"])
        return
    if platform in CONTENT_SYNC_SOURCES:
        content_sync(base, platform)
        return
    if platform == "TikTok Ads":
        status = call(f"{base}/tiktok-ads/status")
        if status["configured"]:
            advertiser = status.get("advertiser_id")
            st.write(f"Configured advertiser · {advertiser}" if advertiser else "TikTok Ads is configured.")
            st.info("TikTok data is fetched on demand. Open Campaigns → TikTok Ads and select Load TikTok report.")
            saved = st.session_state.get(f"tiktok-report:{base}")
            if saved:
                dates, rows = saved
                st.caption(f"Last report in this session · {dates[0]} to {dates[1]} · "
                           f"{len(rows):,} daily campaign records.")
            st.caption("Available data includes spend, impressions, clicks, and conversions by campaign and day.")
        else:
            st.info("TikTok Ads is not connected. An admin can configure an authorized advertiser on the backend.")
        return

    account = select_ad_account(org["id"], base)
    if mode == "demo":
        st.info("Demo accounts use synthetic data. Live synchronization is available in live mode.")
        return
    if account:
        st.caption(f"{account['currency']} · {account['timezone']} · Scheduled or on-demand import")
        route = f"{base}/accounts/{account['id']}"
        if org["role"] == "admin" and st.button("Sync Google Ads data", type="primary"):
            try:
                with st.spinner("Importing Google Ads data…"):
                    call(f"{route}/sync", "POST", timeout=600)
            except APIError as exc:
                st.error(str(exc))
            else:
                st.session_state.flash = "Google Ads synchronization completed."
                st.rerun()

        history = call(f"{route}/sync")
        st.subheader("Latest sync")
        if history:
            latest = history[0]
            st.caption(f"{pretty(latest['status'])} · {latest.get('finished_at') or latest['created_at']}")
            if latest["error"]:
                st.error(latest["error"])
            if latest["counts"]:
                table([{"Data": pretty(key), "Result": sync_result(latest["status"], value), "Records": value}
                       for key, value in latest["counts"].items()])
            else:
                st.info("Record counts are unavailable for this run.")
            if len(history) > 1:
                with st.expander("Earlier runs"):
                    rows = []
                    for run in history[1:]:
                        counts = ", ".join(f"{pretty(key)}: {value:,}" for key, value in (run["counts"] or {}).items())
                        rows.append({"Started": run["created_at"], "Result": pretty(run["status"]),
                                     "Records by type": counts or None, "Details": run["error"]})
                    table(rows)
        else:
            st.info("No sync runs recorded for this account.")
    else:
        st.info("Connect a Google Ads account to import data.")

    if org["role"] == "admin":
        with st.expander("Connect a Google Ads account", expanded=account is None):
            if st.button("Discover accessible accounts"):
                st.session_state[f"discovery:{base}"] = call(f"{base}/discovery", timeout=90)
            options = st.session_state.get(f"discovery:{base}")
            if options:
                mapping = {item["resource"]: item for item in options}
                picked = st.selectbox("Available Google Ads account", list(mapping), format_func=lambda key:
                                      f"{mapping[key]['name']} · {key.split('/')[-1]} · {mapping[key]['currency']}")
                if st.button("Connect account"):
                    call(f"{base}/accounts", "POST", params={"resource": picked}, timeout=90)
                    st.session_state.flash = "Google Ads account connected. Run a sync to import its data."
                    st.rerun()
            elif options is not None:
                st.info("The configured service account returned no eligible Google Ads accounts.")
