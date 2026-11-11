from collections import defaultdict

import streamlit as st

from frontend.client import APIError, call
from frontend.views import number, table


def load_commerce(base, start, end):
    try:
        return call(f"{base}/commerce", params={"start": str(start), "end": str(end)})
    except APIError as exc:
        return {"orders": [], "ga4": [], "coverage": [], "error": str(exc)}


def commerce_totals(data):
    totals = defaultdict(lambda: {"purchases": 0, "revenue": 0, "attributed_purchases": 0, "attributed_revenue": 0})
    for row in data["orders"]:
        for key in totals[row["currency"]]:
            totals[row["currency"]][key] += row[key]
    return dict(totals)


def show_commerce(base, start, end, prior_start, prior_end):
    current = load_commerce(base, start, end)
    previous = load_commerce(base, prior_start, prior_end)
    totals, before = commerce_totals(current), commerce_totals(previous)
    currency = "CAD" if "CAD" in totals or len(totals) != 1 else next(iter(totals))
    values, prior = totals.get(currency, {}), before.get(currency, {})
    for columns, metrics in ((st.columns(2), (("purchases", "Shopify paid orders", "count"), ("revenue", "Shopify revenue", "money"))),
                             (st.columns(2), (("attributed_purchases", "UTM-attributed Shopify orders", "count"),
                                              ("attributed_revenue", "UTM-attributed Shopify revenue", "money")))):
        for column, (key, label, kind) in zip(columns, metrics):
            value, old = values.get(key), prior.get(key)
            delta = number(value - old, kind, currency) if value is not None and old is not None else None
            column.metric(label, number(value, kind, currency) if value is not None else "Not available", delta)
    ga_coverage = [row for row in current["coverage"] if row["source"] == "ga4_store" and row["complete"]]
    sessions = sum(row["sessions"] for row in current["ga4"]) if len(ga_coverage) == 1 else None
    st.metric("Website sessions · GA4", number(sessions, "count", currency) if sessions is not None else "Not available")
    if values.get("purchases"):
        st.caption(f"UTM attribution coverage · {values['attributed_purchases'] / values['purchases']:.1%} of eligible {currency} orders.")
    if current.get("error"):
        st.info(current["error"])
    if any(row.get("stale") for row in current["coverage"]):
        st.warning("Some commerce imports are over 48 hours old. Run the content sync job to refresh them.")
    with st.expander("Commerce sources, coverage, and definitions"):
        st.write(current.get("policy", "No commerce import yet."))
        st.write(current.get("attribution_model", "Order-level attribution is not available."))
        st.caption("Dates outside imported coverage remain unavailable. Revenue stays in its original currency. "
                   "GA4 and Shopify counts are separate and may differ.")
        table(current["coverage"])
        table(current.get("latest_runs", []))
        table([{"Currency": code, **values} for code, values in totals.items()])
        table(current["orders"])
        table(current["ga4"])
    return current
