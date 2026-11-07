import json
import re
from datetime import date, datetime
from urllib.parse import parse_qs, quote, urlsplit

import google.auth
import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request

SHOPIFY_VERSION = "2026-10"
META_METRICS = ("post_media_view", "post_total_media_view_unique", "post_clicks", "post_clicks_by_type")


class ContentError(Exception):
    pass


def response_data(response):
    if not response.is_success:
        raise ContentError(f"Source returned HTTP {response.status_code}. Check its access and configuration.")
    return response.json()


def shopify_token(client, shop, client_id, client_secret):
    return response_data(client.post(
        f"https://{shop}/admin/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    ))["access_token"]


def shopify_articles(client, shop, token, storefront, blog):
    cursor = None
    while True:
        body = response_data(client.post(
            f"https://{shop}/admin/api/{SHOPIFY_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token},
            json={"query": """
                query ContentArticles($after: String) {
                    articles(first: 50, after: $after) {
                        nodes { id title handle publishedAt updatedAt isPublished tags image { url }
                                blog { id handle title } }
                        pageInfo { hasNextPage endCursor }
                    }
                }
            """, "variables": {"after": cursor}},
        ))
        if body.get("errors"):
            raise ContentError("Shopify article query failed. Check the app's content read scope.")
        page = body["data"]["articles"]
        for article in page["nodes"]:
            if article["blog"]["handle"] != blog or not article["isPublished"] or not article["publishedAt"]:
                continue
            if datetime.fromisoformat(article["publishedAt"]) > datetime.now().astimezone():
                continue
            path = f"/blogs/{blog}/{article['handle']}"
            yield {
                "external_id": article["id"], "title": article["title"],
                "url": storefront.rstrip("/") + path, "published_at": article["publishedAt"], "data": article,
            }
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]


def google_headers(url, scope):
    try:
        credentials, _ = google.auth.default(scopes=[f"https://www.googleapis.com/auth/{scope}"])
        headers = {}
        credentials.before_request(Request(), "POST", url, headers)
        return headers
    except GoogleAuthError:
        raise ContentError("Google read credentials are unavailable or invalid.") from None


def metric_row(report, day, timezone, metrics, *, segment="", currency=None):
    return dict(report=report, day=day, timezone=timezone, currency=currency, segment=segment, metrics=metrics)


def analytics_reports(client, property_id, url, start, end, utms):
    endpoint = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    headers = google_headers(endpoint, "analytics.readonly")
    path = urlsplit(url).path
    landing = ("sessions", "engagedSessions", "ecommercePurchases", "purchaseRevenue")
    reports = [("ga4_views", "pagePath", ("screenPageViews", "activeUsers"), [], ""),
               ("ga4_landing", "landingPage", landing, [], "")]
    for utm in utms:
        filters = [("sessionManualSource", utm["utm_source"]), ("sessionManualMedium", utm["utm_medium"]),
                   ("sessionManualCampaignName", utm["utm_campaign"])]
        if utm.get("utm_content"):
            filters.append(("sessionManualAdContent", utm["utm_content"]))
        reports.append(("ga4_facebook", "landingPage", landing, filters, json.dumps(utm, sort_keys=True)))
    for kind, dimension, metrics, extra, segment in reports:
        expressions = [{"filter": {"fieldName": key, "stringFilter": {"value": value, "caseSensitive": True}}}
                       for key, value in [(dimension, path), *extra]]
        result = response_data(client.post(endpoint, headers=headers, json={
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": "date"}], "metrics": [{"name": name} for name in metrics],
            "dimensionFilter": {"andGroup": {"expressions": expressions}} if extra else expressions[0],
        }))
        metadata = result.get("metadata", {})
        # Do not replace prior values with empty rows from a thresholded report.
        if metadata.get("subjectToThresholding") or metadata.get("dataLossFromOtherRow"):
            raise ContentError("GA4 returned a thresholded or aggregated report. Existing data was kept.")
        if metadata.get("schemaRestrictionResponse", {}).get("activeMetricRestrictions"):
            raise ContentError("GA4 restricted one or more metrics. Check access to revenue data.")
        for row in result.get("rows", []):
            day = datetime.strptime(row["dimensionValues"][0]["value"], "%Y%m%d").date()
            values = {key: float(item["value"]) for key, item in zip(metrics, row["metricValues"], strict=True)}
            yield metric_row(kind, day, metadata["timeZone"], values,
                             currency=metadata.get("currencyCode"), segment=segment)


def search_reports(client, site, url, start, end):
    endpoint = f"https://www.googleapis.com/webmasters/v3/sites/{quote(site, safe='')}/searchAnalytics/query"
    headers = google_headers(endpoint, "webmasters.readonly")
    for kind, dimensions in (("gsc_page", ["date"]), ("gsc_query", ["date", "query"])):
        offset = 0
        while True:
            result = response_data(client.post(endpoint, headers=headers, json={
                "startDate": start.isoformat(), "endDate": end.isoformat(), "type": "web",
                "dataState": "final", "aggregationType": "byPage", "dimensions": dimensions,
                "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "equals",
                                                        "expression": url}]}],
                "rowLimit": 25000, "startRow": offset,
            }))
            rows = result.get("rows", [])
            for row in rows:
                yield metric_row(kind, date.fromisoformat(row["keys"][0]), "America/Los_Angeles",
                                 {key: row[key] for key in ("clicks", "impressions", "ctr", "position")},
                                 segment=row["keys"][1] if kind == "gsc_query" else "")
            if len(rows) < 25000:
                break
            offset += len(rows)


def destination(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.hostname in {"l.facebook.com", "lm.facebook.com"}:
            url = parse_qs(parsed.query).get("u", [""])[0]
            parsed = urlsplit(url)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return url
    except ValueError:
        pass
    return None


def facebook_posts(client, page, token, version):
    endpoint = f"https://graph.facebook.com/{version}/{page}/published_posts"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"fields": "id,message,created_time,permalink_url,attachments{url,target,subattachments{url,target}}",
              "limit": 50}
    while True:
        body = response_data(client.get(endpoint, headers=headers, params=params))
        for post in body.get("data", []):
            message = post.get("message") or ""
            links = {url for match in re.findall(r'https?://[^\s<>"\']+', message)
                     if (url := destination(match.rstrip(".,);]")))}
            for attachment in post.get("attachments", {}).get("data", []):
                for item in [attachment, *attachment.get("subattachments", {}).get("data", [])]:
                    for candidate in (item.get("url"), (item.get("target") or {}).get("url")):
                        url = destination(candidate)
                        if url:
                            links.add(url)
            title = message.strip().splitlines()
            yield {"external_id": post["id"], "title": title[0][:180] if title else "Facebook post",
                   "url": destination(post.get("permalink_url")), "published_at": post["created_time"],
                   "data": {"links": sorted(links)}}
        paging = body.get("paging", {})
        if not paging.get("next"):
            return
        params["after"] = paging["cursors"]["after"]


def facebook_metrics(client, post, token, version):
    endpoint = f"https://graph.facebook.com/{version}/{post}"
    headers = {"Authorization": f"Bearer {token}"}
    # Probe independently so one unsupported field cannot erase other observations.
    results = {}
    for field in ("reactions", "comments", "shares"):
        expression = field if field == "shares" else f"{field}.limit(0).summary(true)"
        response = client.get(endpoint, headers=headers, params={"fields": expression})
        if meta_metric_unavailable(response):
            continue
        data = response_data(response).get(field)
        if data is not None:
            count = data.get("count") if field == "shares" else data.get("summary", {}).get("total_count")
            if isinstance(count, int):
                results[field] = count
    for metric in META_METRICS:
        response = client.get(f"{endpoint}/insights", headers=headers, params={"metric": metric, "period": "lifetime"})
        if meta_metric_unavailable(response):
            continue
        for entry in response_data(response).get("data", []):
            values = entry.get("values", [])
            value = entry.get("total_value", {}).get("value", values[-1].get("value") if values else None)
            if value is not None:
                results[metric] = value
    return results


def meta_metric_unavailable(response):
    if response.status_code == 400:
        code = response.json().get("error", {}).get("code")
        return code in {100, 10, 200}
    return False
