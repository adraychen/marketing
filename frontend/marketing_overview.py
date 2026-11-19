from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from backend.metrics import previous_period, ratio, summarize
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


def show_paid_summary(rows):
    """A compact delivery comparison; platform-specific outcomes stay in paid reports."""
    cards = []
    for currency in sorted({r["Currency"] for r in rows if r.get("Spend") is not None}):
        spend = sum(r["Spend"] for r in rows if r.get("Currency") == currency and r.get("Spend") is not None)
        cards.append((f"Reported spend · {currency}", number(spend, "money", currency)))
    if not cards:
        cards.append(("Reported spend", "Not available"))
    for key in ("Impressions", "Clicks"):
        values = [r[key] for r in rows if r.get(key) is not None]
        cards.append((f"Reported {key.lower()}", f"{sum(values):,.0f}" if values else "Not available"))
    columns = st.columns(min(3, len(cards)))
    for index, (label, value) in enumerate(cards):
        columns[index % len(columns)].metric(label, value)

    counts = {platform: sum(r["Platform"] == platform for r in rows) for platform in {r["Platform"] for r in rows}}
    statuses = {"Imported": "Available", "Partial coverage": "Partial period", "Stale import": "Needs refresh",
                "No account connected": "Not connected", "Not connected": "Not connected",
                "No successful paid import yet": "Not synced", "No successful import yet": "Not synced",
                "Report on demand": "Load in Paid Advertising"}
    summary = []
    for row in rows:
        label = row["Platform"]
        if counts[label] > 1:
            label += " · " + (row.get("Account") or "Unknown account")
        currency = row.get("Currency", "")
        summary.append({"Platform": label,
                        "Spend": number(row.get("Spend"), "money", currency),
                        "Impressions": f"{row['Impressions']:,.0f}" if row.get("Impressions") is not None else "—",
                        "Clicks": f"{row['Clicks']:,.0f}" if row.get("Clicks") is not None else "—",
                        "CTR": number(ratio(row.get("Clicks"), row.get("Impressions")), "percent", currency),
                        "Avg. CPC": number(ratio(row.get("Spend"), row.get("Clicks")), "money", currency),
                        "Data status": statuses.get(row.get("Status"), "Unavailable")})
    table(summary)
    st.caption("Reported totals include available data only. — means unavailable or an undefined ratio; 0 means reported zero. "
               "CTR and average CPC are calculated from reported clicks, impressions and spend. Meta clicks include all ad clicks.")
    st.caption("Open Paid Advertising for campaign results, daily charts, conversions, revenue and ROAS. "
               "Each platform uses its own conversion definitions and reporting time zone.")
    if any(row.get("Status") != "Imported" for row in rows):
        with st.expander("Data availability"):
            table([{"Platform": row["Platform"], "Account": row.get("Account"), "Status": row.get("Status")}
                   for row in rows if row.get("Status") != "Imported"])


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
    try:
        from frontend.chatgpt_ads import load as load_chatgpt, totals as chatgpt_totals
        data = load_chatgpt(base, start, end)
        if data["account"]:
            values = chatgpt_totals(data["daily"])
            rows.append({"Platform": "ChatGPT Ads", "Account": data["account"].get("name"),
                         "Currency": data["account"]["currency_code"],
                         "Status": "Stale import" if data["stale"] else "Imported" if data["coverage"] else "Partial coverage",
                         "Spend": values["spend"], "Impressions": values["impressions"], "Clicks": values["clicks"],
                         "ChatGPT goal conversions": values["conversions"], "ChatGPT attributed sales": values["order_created_attributed_sales"]})
        else:
            rows.append({"Platform": "ChatGPT Ads", "Status": "No successful import yet"})
    except APIError:
        rows.append({"Platform": "ChatGPT Ads", "Status": "Not connected or migration needed"})
    show_paid_summary(rows)
    if paid_only:
        return

    st.subheader("Organic marketing")
    try:
        catalog = call(f"{base}/content")
    except APIError as exc:
        st.warning(str(exc))
        return
    from frontend.organic_summary import show_overview
    show_overview(base, catalog, start, end)
