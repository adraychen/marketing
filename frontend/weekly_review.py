from datetime import date, timedelta

import streamlit as st

from backend.review import MIN_CLICKS, weekly_review
from frontend.client import APIError, call
from frontend.views import number, pretty, table

OVERVIEW = (
    ("spend", "Spend", "money"), ("conversions", "Conversions", "decimal"),
    ("value", "Conversion value", "money"), ("roas", "ROAS", "multiple"),
)


def period_text(first, last):
    return f"{date.fromisoformat(first):%b %d, %Y} – {date.fromisoformat(last):%b %d, %Y}"


def signed(value, kind, currency):
    if value is None:
        return "—"
    prefix = "+" if value > 0 else "-" if value < 0 else ""
    return prefix + number(abs(value), kind, currency)


def show_weekly_review(account, route, latest_end, ai_available):
    st.title("Weekly performance review")
    st.caption("Compare two seven-day periods in your Google Ads account timezone.")
    end = st.date_input("Week ending", latest_end, min_value=date(2000, 1, 14),
                        max_value=latest_end, key=f"review-end:{route}")
    start = end - timedelta(days=6)
    snapshot = call(f"{route}/report", params={"start": start.isoformat(), "end": end.isoformat()})
    review = weekly_review(snapshot, account, start, end)
    st.caption(f"{period_text(review['start'], review['end'])} vs. "
               f"{period_text(review['prior_start'], review['prior_end'])}")
    if not review["coverage"]:
        st.info("The review needs two full weeks of imported data. Choose an earlier week or sync the account.")
        return
    if not review["has_activity"]:
        st.info("Google Ads returned no campaign activity for either week.")
        return

    currency = account["currency"]
    for column, (key, label, kind) in zip(st.columns(4), OVERVIEW):
        now, before = review["current"][key], review["previous"][key]
        delta = signed(now - before, kind, currency) if now is not None and before is not None else None
        column.metric(label, number(now, kind, currency), delta, delta_color="off", border=True)
    st.caption("Recent conversions can arrive later. Conversion value and ROAS use Google Ads attribution.")
    current_clicks = review["current"]["clicks"]
    prior_clicks = review["previous"]["clicks"]
    ai_ready = review.get("ai_ready", min(current_clicks, prior_clicks) >= MIN_CLICKS)
    if not ai_ready:
        st.info(f"Low traffic · {current_clicks:,} clicks this week and {prior_clicks:,} last week. "
                f"The AI summary needs at least {MIN_CLICKS} clicks in each week. "
                "Use the figures below to see what was reported.")

    st.subheader("Actions to review")
    if not review["actions"]:
        st.info("No changes met the review thresholds for these weeks.")
    for position, action in enumerate(review["actions"], 1):
        with st.container(border=True):
            st.text(f"{position}. {action['label']}")
            now, before = action["current"], action["previous"]
            if action["kind"] == "search_term":
                context = " · ".join(filter(None, (action["campaign"], action["ad_group"])))
                if context:
                    st.text(context)
                st.write(f"{number(now['spend'], 'money', currency)} spent on {now['clicks']:,} clicks "
                         "this week, with zero reported conversions in both weeks.")
                st.write("Check whether the query fits the campaign. If it is irrelevant, review adding it "
                         "as a negative keyword in Google Ads.")
            else:
                st.write(f"Spend rose from {number(before['spend'], 'money', currency)} to "
                         f"{number(now['spend'], 'money', currency)} while conversions fell from "
                         f"{number(before['conversions'], 'decimal', currency)} to "
                         f"{number(now['conversions'], 'decimal', currency)}.")
                st.write("Check conversion lag and compare search terms or placements before changing the budget.")
    st.caption(f"Signals require at least {MIN_CLICKS} current-week clicks. Campaign signals also require "
               "at least three prior conversions. Search-term spend must reach prior account CPA when available. "
               "Items are ranked by spend or spend increase and can overlap.")

    st.subheader("What drove the change")
    dimension = st.radio("Breakdown", ("Campaigns", "Keywords", "Search terms"), horizontal=True)
    level = dimension.lower().replace(" ", "_")
    sort_label = st.selectbox("Rank by absolute change in", ("Spend", "Conversions", "Conversion value"))
    metric = {"Spend": "spend_delta", "Conversions": "conversion_delta", "Conversion value": "value_delta"}[sort_label]
    entries = [entry for entry in review["drivers"][level] if entry[metric] is not None]
    entries.sort(key=lambda entry: abs(entry[metric]), reverse=True)
    rows = []
    for entry in entries[:10]:
        now, before = entry["current"], entry["previous"]
        row = {dimension: entry["label"]}
        if level == "campaigns":
            row.update({"Campaign type": pretty(entry["campaign_type"]), "Current status": pretty(entry["status"]),
                        "Current daily budget": number(entry["daily_budget"], "money", currency)})
        else:
            row.update({"Campaign": entry["campaign"], "Ad group": entry["ad_group"]})
            if level == "keywords":
                row["Match type"] = pretty(entry["match_type"])
        row.update({
            "Spend": number(now["spend"], "money", currency),
            "Spend change": signed(entry["spend_delta"], "money", currency),
            "Conversions": number(now["conversions"], "decimal", currency),
            "Conversion change": signed(entry["conversion_delta"], "decimal", currency),
            "Value change": signed(entry["value_delta"], "money", currency),
            "Avg. CPC (prior → current)": f"{number(before['cpc'], 'money', currency)} → "
                                         f"{number(now['cpc'], 'money', currency)}",
            "ROAS (prior → current)": f"{number(before['roas'], 'multiple', currency)} → "
                                      f"{number(now['roas'], 'multiple', currency)}",
        })
        rows.append(row)
    table(rows)
    if level == "search_terms":
        st.caption("Google reports only some search terms. Their totals may differ from campaign totals.")

    if ai_available and ai_ready:
        st.subheader("AI campaign analysis")
        st.caption("Groq receives aggregate metrics and anonymized campaign evidence.")
        focuses = {
            "performance": "Interpret campaign performance",
            "strategies": "Compare advertising strategies",
            "decline": "Investigate declining results",
            "optimization": "Recommend optimizations",
        }
        focus = st.selectbox("Analysis focus", list(focuses), format_func=focuses.get)
        key = f"review-ai:{route}:{end}:{focus}"
        if st.button("Generate AI analysis"):
            try:
                with st.spinner("Summarizing the review…"):
                    st.session_state[key] = call(f"{route}/weekly-review/ai", "POST",
                                                 params={"end": end.isoformat(), "focus": focus}, timeout=60)
            except APIError as exc:
                st.warning(str(exc))
        if key in st.session_state:
            result = st.session_state[key]
            if result.get("model"):
                st.caption(f"Groq · {result['model']}")
            st.text(result["summary"])
            if result.get("campaign_labels"):
                with st.expander("Campaign names"):
                    for alias, name in result["campaign_labels"].items():
                        st.text(f"{alias} · {name}")
