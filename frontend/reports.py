import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

import streamlit as st

from backend.metrics import previous_period, summarize
from backend.models import Creative
from backend.review import MIN_CLICKS, weekly_review
from backend.validation import validate
from frontend.client import call
from frontend.content_marketing import GA_LABELS, SOURCE_LABELS, grouped
from frontend.views import coverage, metric_by_resource, period_rows

INLINE_LIMIT = 200_000
FOCUSES = ("performance", "strategies", "decline", "optimization")
ARCHIVE_SUMMARIES = {"Report scope", "Google Ads coverage", "Google Ads overview", "Weekly review scope",
                     "Weekly review totals", "Content source status", "Marketing fact verification",
                     "Google Ads budget and bidding"}


def cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def markdown_cell(value):
    return (escape(cell(value), quote=False).replace("\\", "\\\\").replace("|", "\\|")
            .replace("\r", "\\r").replace("\n", "\\n"))


def csv_cell(value):
    rendered = cell(value)
    if isinstance(value, str) and rendered.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + rendered
    return rendered


class Report:
    def __init__(self):
        self.sections = []

    def note(self, text):
        self.sections.append((None, text))

    def table(self, name, rows):
        self.sections.append((name, rows))

    def export(self):
        intro = [
            "# Okoo Marketing report",
            "Data cells contain reported values. Empty cells mean unavailable or undefined. "
            "An empty table means the source returned no rows for that section. Check sync status and coverage.",
            "Ratios are decimal fractions except ROAS, which is a multiple. Google Ads conversion rate uses "
            "interactions. Conversion value and ROAS use Google Ads attribution.",
            "GA4 active users can recur on different days. Facebook-tagged sessions can overlap the GA4 landing "
            "report. Facebook Insights are lifetime observations and should never be summed across dates. "
            "Search Console query rows may omit some queries.",
        ]
        inline = list(intro)
        archive = list(intro)
        tables = []
        for name, rows in self.sections:
            if name is None:
                inline.append(rows)
                archive.append(rows)
                continue
            heading = f"## {name}\n\n{len(rows):,} rows"
            if not rows:
                inline.append(heading)
                archive.append(heading)
                continue
            columns = list(dict.fromkeys(key for row in rows for key in row))
            lines = [
                "| " + " | ".join(map(markdown_cell, columns)) + " |",
                "| " + " | ".join("---" for _ in columns) + " |",
            ]
            lines.extend("| " + " | ".join(markdown_cell(row.get(key)) for key in columns) + " |" for row in rows)
            inline.append(heading + "\n\n" + "\n".join(lines))
            slug = name.lower().replace(" ", "-")
            filename = f"{len(tables) + 1:02d}-{slug}.csv"
            tables.append((filename, columns, rows))
            archive_section = heading + f" · [CSV table]({filename})"
            if name in ARCHIVE_SUMMARIES:
                archive_section += "\n\n" + "\n".join(lines)
            archive.append(archive_section)
        document = "\n\n".join(inline).encode("utf-8")
        if len(document) <= INLINE_LIMIT:
            return document, "md", "text/markdown"

        output = io.BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as bundle:
            archive.insert(1, "Read this file together with all linked CSV files. A leading apostrophe on "
                           "formula-like text keeps spreadsheet apps from evaluating it.")
            bundle.writestr("report.md", "\n\n".join(archive))
            for filename, columns, rows in tables:
                buffer = io.StringIO(newline="")
                writer = csv.writer(buffer)
                writer.writerow(columns)
                writer.writerows([csv_cell(row.get(key)) for key in columns] for row in rows)
                bundle.writestr(filename, buffer.getvalue().encode("utf-8-sig"))
        return output.getvalue(), "zip", "application/zip"


def split_metrics(row):
    return {**{key: value for key, value in row.items() if key != "metrics"},
            **{f"metric_{key}": value for key, value in row["metrics"].items()}}


def draft_row(row):
    try:
        issues = validate(Creative.model_validate(row["creative"]))
    except ValueError:
        issues = ["The saved creative exceeds the supported asset or URL limits."]
    return {**row, "validation_issues": issues}


def campaign_settings(campaigns, currency):
    return [
        {"campaign": row["data"].get("name") or row["resource"], "status": row["data"].get("status"),
         "daily_budget": row["data"].get("daily_budget"), "currency": currency,
         **{key: row["data"].get(key) for key in ("budget_period", "total_budget", "budget_resource",
              "shared_budget", "bidding_strategy", "portfolio_strategy", "max_cpc_ceiling")}}
        for row in campaigns
    ]


def fact_checks(settings, first_activity=None):
    rows = []
    for fact, field, check in (
        ("Google Ads budget per campaign", "daily_budget", "Check the campaign budget and its period in Google Ads."),
        ("Google Ads bidding strategy per campaign", "bidding_strategy", "Check bidding settings or refresh the Google Ads sync."),
    ):
        count = sum(
            row[field] not in (None, "", "UNKNOWN", "UNSPECIFIED")
            or (field == "daily_budget" and row["total_budget"] is not None)
            for row in settings
        )
        status = "Verified from latest import" if count and count == len(settings) else (
            "Partially verified" if count else "Needs verification"
        )
        rows.append({"fact": fact, "status": status, "evidence": f"{count} of {len(settings)} imported campaigns",
                     "follow_up": check if not count or count < len(settings) else None})
    rows.extend([
        {"fact": "Main marketing objective for the next 30 days", "status": "Needs verification",
         "evidence": "The dashboard has no connected source for marketing objectives.",
         "follow_up": "Identify the current approved marketing plan or goal."},
        {"fact": "Has Okoo received any Shopify orders?", "status": "Needs verification",
         "evidence": "The Shopify sync imports articles. GA4 article-attributed purchases do not establish storewide order history.",
         "follow_up": "Check Shopify Orders or connect a source with read access and sufficient order history."},
        {"fact": "Citizen Collection campaign activation date", "status": "Needs verification",
         "evidence": (f"First recorded activity in the selected period was {first_activity}. Activation date is unverified."
                      if first_activity else "The Google Ads import does not include campaign activation history."),
         "follow_up": "Confirm the exact campaign and check when it was enabled in Google Ads change history."},
    ])
    return rows


def add_ads(report, account, route, params, start, end, week_end, terms, drafts):
    snapshot = call(f"{route}/report", params=params)
    currency = account["currency"]
    report.note("Campaign budgets and bidding strategies reflect the latest Google Ads import. "
                "Match shared budgets by budget resource. A campaign total budget covers the campaign duration. "
                "Daily budgets are average daily amounts. Do not sum shared budgets.")
    settings = campaign_settings(snapshot["campaigns"], currency)
    report.table("Google Ads budget and bidding", settings)
    report.note("The Maximize Clicks ceiling is the campaign-level cap when returned by Google Ads. "
                "Configured manual CPC bids are in the ad group and keyword tables. A missing ceiling does not "
                "establish a cap of zero. Portfolio strategy caps are not available from this import.")
    matches = [row["resource"] for row in snapshot["campaigns"]
               if (row["data"].get("name") or "").strip().casefold() == "citizen collection"]
    first_activity = None
    if len(matches) == 1:
        first_activity = min(
            (row["day"] for row in period_rows(snapshot, "campaigns", start, end)
             if row["resource"] == matches[0] and (row["impressions"] or row["clicks"] or row["spend"])),
            default=None,
        )
    checks = fact_checks(settings, first_activity)
    missing_caps = [
        row["campaign"] for row in settings
        if row["bidding_strategy"] in ("TARGET_SPEND", "MAXIMIZE_CLICKS", None, "", "UNKNOWN", "UNSPECIFIED")
        and row["max_cpc_ceiling"] is None
    ]
    if missing_caps:
        checks.append({"fact": "Google Ads maximum CPC ceiling", "status": "Needs verification",
                       "evidence": {"campaigns_without_verified_ceiling": missing_caps},
                       "follow_up": "Check whether a cap is set in the campaign or portfolio bidding settings."})
    report.table("Marketing fact verification", checks)
    prior_start, prior_end = previous_period(start, end)
    complete = coverage(account, start, end)
    comparable = coverage(account, prior_start, end)
    report.table("Google Ads coverage", [{
        "account": account["name"], "currency": currency, "timezone": account["timezone"],
        "synced_from": account["synced_from"], "synced_through": account["synced_through"],
        "selected_period_complete": complete, "previous_period_available": comparable,
        "previous_start": prior_start.isoformat(), "previous_end": prior_end.isoformat(),
    }])
    totals, daily = [], []
    periods = [("Selected", start, end)]
    if comparable:
        periods.append(("Previous", prior_start, prior_end))
    for label, first, last in periods:
        rows = period_rows(snapshot, "campaigns", first, last)
        totals.append({"period": label, "start": first.isoformat(), "end": last.isoformat(),
                       "reported_rows": len(rows), **(summarize(rows, currency) if rows else {})})
        by_day = defaultdict(list)
        for row in rows:
            by_day[row["day"]].append(row)
        daily.extend({"period": label, "day": day, **summarize(entries, currency)}
                     for day, entries in sorted(by_day.items()))
    report.table("Google Ads overview", totals)
    report.table("Google Ads daily trend", daily)
    levels = ["campaigns", "ad_groups", "ads", "keywords"]
    if terms:
        levels.append("search_terms")
    for level in levels:
        metrics = metric_by_resource(snapshot, level, start, end, currency)
        report.table("Google Ads " + level.replace("_", " "), [
            {"resource": row["resource"], "parent": row.get("parent"), **row["data"],
             **{f"metric_{key}": value for key, value in metrics.get(row["resource"], {}).items()}}
            for row in snapshot[level]
        ])
    report.table("Google Ads daily resource metrics", [
        row for row in snapshot["performance"] if row["kind"] in levels
        and (comparable or row["day"] >= start.isoformat())
    ])
    history = call(f"{route}/sync")
    report.table("Google Ads sync history", [
        {key: run.get(key) for key in ("status", "counts", "created_at", "finished_at")}
        for run in history
    ])
    if drafts:
        report.note("Draft data is the saved revision. Unsaved form edits are excluded.")
        report.table("Saved ad drafts", [draft_row(row) for row in call(f"{route}/drafts")])

    week_start = week_end - timedelta(days=6)
    weekly = call(f"{route}/report", params={"start": week_start.isoformat(), "end": week_end.isoformat()})
    review = weekly_review(weekly, account, week_start, week_end)
    report.table("Weekly review scope", [{key: review[key] for key in
                 ("start", "end", "prior_start", "prior_end", "coverage", "has_activity", "ai_ready")}])
    if not review["has_activity"]:
        report.note("Weekly review has no comparison. Two imported weeks and campaign activity are required.")
        return
    report.table("Weekly review totals", [
        {"period": label, **review[label]} for label in ("current", "previous")
    ])
    for level, rows in review["drivers"].items():
        if level != "search_terms" or terms:
            report.table("Weekly review " + level.replace("_", " "), rows)
    report.table("Weekly review actions", [row for row in review["actions"]
                 if row["kind"] != "search_term" or terms])
    if not review["ai_ready"]:
        report.note(f"AI analysis unavailable for this comparison. The selected week has {review['current']['clicks']} "
                    f"clicks and the previous week has {review['previous']['clicks']}. "
                    f"Each week needs at least {MIN_CLICKS} clicks.")
        return
    analyses = []
    for focus in FOCUSES:
        result = st.session_state.get(f"review-ai:{route}:{week_end}:{focus}")
        if isinstance(result, dict):
            analyses.append({"focus": focus, "model": result.get("model"), "summary": result["summary"],
                             "campaign_labels": result.get("campaign_labels")})
    legacy = st.session_state.get(f"review-ai:{route}:{week_end}")
    if not analyses and isinstance(legacy, str):
        analyses.append({"focus": "summary", "model": None, "summary": legacy})
    if not analyses:
        report.note("No AI interpretation for these weeks is available in this session.")
    report.table("Existing AI interpretations", analyses)


def add_content(report, route, catalog, campaign, params, terms=False):
    items = [item for item in catalog["items"] if not campaign or campaign in item["campaigns"]]
    report.table("Published content catalog", items)
    sources = {row["source"]: row for row in catalog["sources"]}
    report.table("Content source status", [
        {"source": source, "label": label, "status": sources.get(source, {}).get("status", "No sync recorded"),
         "records": sources.get(source, {}).get("records"),
         "finished_at": sources.get(source, {}).get("finished_at")}
        for source, label in SOURCE_LABELS.items()
    ])
    daily, snapshots, post_links = [], [], []
    for item in items:
        details = call(f"{route}/items/{item['id']}", params=params)
        context = {"item_id": item["id"], "title": item["title"]}
        daily.extend({**context, **split_metrics(row)} for row in details["daily"]
                     if terms or row["report"] != "gsc_query")
        snapshots.extend({**context, **split_metrics(row)} for row in details["snapshots"])
        post_links.extend({**context, **row} for row in details["links"])
    totals = []
    for keys, rows in grouped(daily, "item_id", "title", "report", "source_account",
                              "segment", "currency", "timezone").items():
        item_id, title, kind, source, segment, currency, zone = keys
        total = {"item_id": item_id, "title": title, "report": kind, "source_account": source,
                 "segment": segment, "currency": currency, "timezone": zone}
        if kind.startswith("ga4_"):
            for metric in GA_LABELS.keys() - {"activeUsers"}:
                column = f"metric_{metric}"
                if any(column in row for row in rows):
                    total[column] = sum(row.get(column, 0) for row in rows)
        elif kind == "gsc_page":
            impressions = sum(row["metric_impressions"] for row in rows)
            clicks = sum(row["metric_clicks"] for row in rows)
            position = sum(row["metric_position"] * row["metric_impressions"] for row in rows)
            total.update(metric_clicks=clicks, metric_impressions=impressions,
                         metric_ctr=clicks / impressions if impressions else None,
                         metric_position=position / impressions if impressions else None)
        else:
            continue
        totals.append(total)
    report.table("Content performance totals", totals)
    report.table("GA4 and Search Console daily metrics", daily)
    report.table("Facebook Insights lifetime observations", snapshots)
    report.table("Content links and UTM tags", post_links)
    products, ads = [], []
    for entry in catalog["campaigns"]:
        slug = entry["slug"]
        if not campaign or slug == campaign:
            links = call(f"{route}/campaigns/{slug}/links")
            products.extend({"content_campaign": slug, **row} for row in links["products"])
            ads.extend({"content_campaign": slug, **row} for row in links["ads"])
    report.table("Linked Shopify products", products)
    report.table("Linked Google Ads campaigns", ads)


def show_reports(org, base, account, mode):
    st.title("Reports")
    st.caption("Export synced metrics for people and AI tools. Large reports include full CSV tables.")
    content_route = f"{base}/content"
    catalog = call(content_route)
    campaigns = {entry["slug"]: entry["name"] for entry in catalog["campaigns"]}
    campaign = st.selectbox("Content campaign", ["", *campaigns],
                            format_func=lambda key: campaigns.get(key, "All discovered content"))
    today = datetime.now(ZoneInfo(account["timezone"] if account else "America/Toronto")).date()
    last = today - timedelta(days=1)
    dates = st.date_input("Report date range", (last - timedelta(days=29), last),
                          min_value=date(2000, 1, 1), max_value=today)
    if len(dates) != 2:
        st.info("Select both report dates.")
        return
    start, end = dates
    if (end - start).days > 365:
        st.info("Choose a date range of up to 366 days.")
        return
    week_end = st.date_input("Week ending for Weekly Review", max(date(2000, 1, 14), min(end, last)),
                             min_value=date(2000, 1, 14), max_value=last) if account else None
    terms = st.checkbox("Include Google Ads search terms and Search Console queries")
    drafts = st.checkbox("Include saved ad drafts", disabled=account is None)
    st.caption("Includes performance totals, daily rows, Weekly Review, content links, and sync status. "
               "Existing AI results for the selected week are included when available in this session.")
    scope = (org["id"], account["id"] if account else None, campaign, start, end, week_end, terms, drafts)
    if st.button("Generate report", type="primary"):
        st.session_state.pop("generated-report", None)
        with st.spinner("Preparing report…"):
            report = Report()
            report.table("Report scope", [{
                "organization": org["name"], "mode": mode, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "google_ads_account": account["name"] if account else None,
                "content_campaign": campaigns.get(campaign, "All discovered content"),
                "start": start.isoformat(), "end": end.isoformat(),
                "includes_search_terms": terms, "includes_saved_drafts": drafts,
            }])
            params = {"start": start.isoformat(), "end": end.isoformat()}
            if account:
                add_ads(report, account, f"{base}/accounts/{account['id']}", params, start, end,
                        week_end, terms, drafts)
            else:
                report.note("No Google Ads account is connected.")
                report.table("Marketing fact verification", fact_checks([]))
            add_content(report, content_route, catalog, campaign, params, terms)
            contents = [{"Section": name, "Rows": len(rows)} for name, rows in report.sections if name]
            st.session_state["generated-report"] = scope, report.export(), contents
    saved = st.session_state.get("generated-report")
    if saved and saved[0] == scope:
        data, extension, mime = saved[1]
        with st.expander("Report contents"):
            st.dataframe(saved[2], hide_index=True)
        if extension == "zip":
            st.info("Unzip the report and share report.md together with all its CSV files.")
        st.download_button("Download report", data, f"okoo-marketing-{start}-{end}.{extension}", mime)
