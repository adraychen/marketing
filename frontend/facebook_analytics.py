from collections import defaultdict
from datetime import date, timedelta

import streamlit as st

from backend.facebook_analytics import AUDIENCE, CONTENT_MANUAL, PAGE_MANUAL, PAGE_METRICS
from frontend.client import APIError, call
from frontend.views import table

MANUAL_ONLY = {
    "all_views", "posts_views", "reel_views", "reel_watch_time", "page_organic_views", "page_paid_views",
    "outbound_clicks", "reel_unique_viewers", "reel_retention",
    "business_video_views_3s", "business_video_views_1m",
}
DAILY_ADDITIVE = set(PAGE_METRICS) - {
    "page_total_media_view_unique", "page_daily_follows_unique", "page_daily_unfollows_unique",
}


def page_rows(data, start, end):
    latest = {}
    for row in data["page"]:
        key = row["page_id"], row["metric"], row["day"], row["basis"]
        if key not in latest or row["collected_at"] > latest[key]["collected_at"]:
            latest[key] = row
    rows = []
    grouped = defaultdict(list)
    follows = defaultdict(dict)
    for row in latest.values():
        metric = row["metric"]
        if metric not in PAGE_MANUAL:
            continue
        label, unit = PAGE_MANUAL[metric]
        rows.append({**row, "label": label, "unit": unit, "source": "Facebook Graph API",
                     "period_start": row["day"], "period_end": row["day"]})
        if metric in DAILY_ADDITIVE and row["basis"] == "day":
            grouped[row["page_id"], metric, row["api_version"], row["reporting_offset"]].append(row)
        if metric in {"page_daily_follows_unique", "page_daily_unfollows_unique"} and row["basis"] == "day":
            follows[row["page_id"], row["day"], row["api_version"], row["reporting_offset"]][metric] = row
    for (page, day, version, offset), pair in follows.items():
        added = pair.get("page_daily_follows_unique")
        removed = pair.get("page_daily_unfollows_unique")
        if added and removed:
            rows.append({**added, "metric": "net_follows", "label": "Net follows (daily follows minus unfollows)",
                         "value": added["value"] - removed["value"], "unit": "people",
                         "source": "Calculated from Facebook Graph API", "period_start": day, "period_end": day})
    if start < end:
        for (_, metric, _, _), days in grouped.items():
            if len(days) != (date.fromisoformat(end) - date.fromisoformat(start)).days + 1:
                continue
            last = max(days, key=lambda row: row["day"])
            label, unit = PAGE_MANUAL[metric]
            rows.append({**last, "basis": "period", "label": label, "unit": unit,
                         "source": "Calculated from Facebook Graph API daily values",
                         "value": sum(row["value"] for row in days), "period_start": start, "period_end": end})
    return rows


def manual_rows(data, rows, snapshots_by_item):
    result = []
    for observation in data["manual"]:
        item = observation["item_id"]
        metrics = CONTENT_MANUAL if item else PAGE_MANUAL
        row = {**observation, "platform": "Facebook", "label": metrics[observation["metric"]][0],
               "source": "Manual / Facebook Business Dashboard", "api_value": None,
               "comparison": "Comparable API value unavailable"}
        if not item:
            for candidate in rows:
                same_basis = candidate["basis"] == observation["basis"] or (
                    candidate["basis"] == "day" and observation["basis"] == "period")
                same_offset = candidate["basis"] == "point" or (
                    observation["reporting_offset"] and observation["reporting_offset"] == candidate["reporting_offset"])
                if same_basis and same_offset and all(candidate[key] == observation[key] for key in (
                    "page_id", "metric", "unit", "period_start", "period_end"
                )):
                    row["api_value"] = candidate["value"]
                    row["comparison"] = ("Same day, collection times may differ" if candidate["basis"] == "point"
                                         else "Same metric and reporting period")
                    break
        elif observation["basis"] == "lifetime":
            from frontend.visibility import facebook_latest

            snapshots = [snapshot for snapshot in snapshots_by_item.get(item, [])
                         if snapshot.get("observed_day") == observation["observed_day"]]
            value = facebook_latest(snapshots, "video").get(observation["metric"])
            if type(value) in (int, float):
                row["api_value"] = value
                row["comparison"] = "Same day lifetime values, collection times may differ"
        if row["api_value"] is not None:
            row["manual_minus_api"] = observation["value"] - row["api_value"]
        result.append(row)
    return result


def availability_rows(data, rows):
    available = {(row["page_id"], row["metric"]) for row in rows}
    statuses = {(row["page_id"], row["metric"]): row for row in data["availability"]}
    return [
        {"page_id": page, "metric": metric, "label": label,
         "latest_api_status": statuses.get((page, metric), {}).get(
             "availability", "Manual only in this dashboard" if metric in MANUAL_ONLY else "Not yet synchronized"),
         "available_in_selected_dates": (page, metric) in available, "manual_entry": True,
         "collected_at": statuses.get((page, metric), {}).get("collected_at")}
        for page in data["pages"] for metric, (label, _) in PAGE_MANUAL.items()
    ]


def edit_manual(route, data, items, start, end):
    if not data["can_edit"] or not data["pages"]:
        return
    saved = {row["id"]: row for row in data["manual"]}
    selected = st.selectbox("Manual observation", [None, *saved], format_func=lambda key: "New observation" if key is None
                            else f"{saved[key]['content_title'] or 'Page'} · {saved[key]['metric']} · {saved[key]['period_end']}")
    row = saved.get(selected, {})
    if selected and st.checkbox("Confirm deletion of this manual observation", key=f"confirm-facebook-{selected}"):
        if st.button("Delete observation", key=f"delete-facebook-{selected}"):
            call(f"{route}/manual/{selected}", "DELETE")
            st.rerun()
    prefix = f"facebook-manual-{route}-{selected}"
    pages = data["pages"]
    page_id = row.get("page_id")
    page = st.selectbox("Facebook Page", pages, index=pages.index(page_id) if page_id in pages else 0,
                        key=prefix + "-page")
    posts = {item["id"]: item["title"] for item in items
             if item["source"] == "facebook" and item.get("source_account") == page}
    options = [None, *posts]
    item_id = row.get("item_id")
    item = st.selectbox("Scope", options, index=options.index(item_id) if item_id in options else 0,
                        format_func=lambda key: posts[key] if key else "Facebook Page", key=prefix + "-scope")
    metrics = CONTENT_MANUAL if item else PAGE_MANUAL
    names = list(metrics)
    metric = st.selectbox("Metric", names, index=names.index(row["metric"]) if row.get("metric") in metrics else 0,
                          format_func=lambda key: metrics[key][0], key=prefix + "-metric")
    basis_options = ["period", "lifetime"] if item else ["point"] if metric == "followers_count" else ["period"]
    basis = st.selectbox("Observation basis", basis_options,
                         index=basis_options.index(row["basis"]) if row.get("basis") in basis_options else 0,
                         key=prefix + "-basis")
    with st.form(prefix + metric + basis):
        unit = metrics[metric][1]
        factor = 1000 if unit == "milliseconds" else 1
        value = st.number_input(f"Value ({'seconds' if factor == 1000 else unit})",
                                value=float(row["value"]) / factor if row.get("metric") == metric else 0.0)
        observed = st.date_input("Observation date", date.fromisoformat(row["observed_day"]) if row else date.today())
        first = st.date_input("Period start", date.fromisoformat(row.get("period_start", start))) if basis == "period" else observed
        last = st.date_input("Period end", date.fromisoformat(row.get("period_end", end))) if basis == "period" else observed
        offset = st.text_input("Reporting UTC offset (optional, such as -08:00)", row.get("reporting_offset", ""))
        notes = st.text_area("Notes (optional)", row.get("notes", ""), max_chars=1000)
        st.caption("Source · Manual / Facebook Business Dashboard")
        if st.form_submit_button("Save observation"):
            call(f"{route}/manual/{selected}" if selected else f"{route}/manual", "PUT" if selected else "POST",
                 payload={"page_id": page, "item_id": item, "metric": metric, "value": value * factor,
                          "basis": basis, "period_start": str(first), "period_end": str(last),
                          "observed_day": str(observed), "reporting_offset": offset.strip(), "notes": notes})
            st.rerun()


def show_facebook_analytics(content_route, items):
    route = f"{content_route}/facebook-analytics"
    today = date.today()
    dates = st.date_input("Facebook analytics dates", (today - timedelta(days=29), today), max_value=today,
                          key=f"facebook-analytics-dates:{content_route}")
    if len(dates) != 2:
        return
    start, end = map(str, dates)
    try:
        data = call(route, params={"start": start, "end": end})
        rows = page_rows(data, start, end)
        overview, availability = st.tabs(("Page overview", "Data availability and manual entry"))
        with overview:
            st.caption("Daily values use Meta reporting boundaries. Follower totals are observations as of collection. "
                       "Unique viewers and overlapping Page, post, and reel views are shown separately.")
            latest_followers = {}
            series = defaultdict(list)
            for row in rows:
                if row["metric"] == "followers_count":
                    latest_followers[row["page_id"]] = row
                if row["basis"] in {"day", "point"}:
                    series[row["page_id"], row["metric"], row["api_version"]].append(row)
            for page, row in latest_followers.items():
                st.metric(f"Followers · {page}", row["value"])
                st.caption(f"Observed on {row['day']}")
            if series:
                selection = st.selectbox("Trend", list(series),
                                         format_func=lambda key: f"{PAGE_MANUAL[key[1]][0]} · {key[0]} · {key[2]}")
                st.line_chart([{"day": row["day"], "value": row["value"]} for row in series[selection]],
                              x="day", y="value")
            table(rows)
            page_manual = manual_rows({**data, "manual": [row for row in data["manual"] if not row["item_id"]]},
                                      rows, {})
            if page_manual:
                st.subheader("Manual Page observations")
                table(page_manual)
        with availability:
            table(availability_rows(data, rows))
            st.caption("For content metrics and their API status, open the post or reel under Content performance.")
            item_ids = {row["item_id"] for row in data["manual"] if row["item_id"] and row["basis"] == "lifetime"}
            snapshots = {
                item: call(f"{content_route}/items/{item}", params={"start": start, "end": end})["snapshots"]
                for item in item_ids
            }
            manual = manual_rows(data, rows, snapshots)
            table(manual)
            edit_manual(route, data, items, start, end)
    except APIError as exc:
        st.warning(str(exc))
