import json
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit

import google.auth
import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request

SHOPIFY_VERSION = "2026-10"
META_METRICS = ("post_media_view", "post_total_media_view_unique", "post_clicks", "post_clicks_by_type")
META_VIDEO_METRICS = ("total_video_views", "total_video_view_total_time", "total_video_avg_time_watched",
                      "total_video_retention_graph", "total_video_60s_excludes_shorter_views")


class ContentError(Exception):
    pass


def response_data(response):
    if not response.is_success:
        if response.status_code == 401:
            raise ContentError("Authentication failed (HTTP 401). Check the existing credential.")
        if response.status_code == 403:
            raise ContentError("Access denied (HTTP 403). Check API enablement and account or property access.")
        raise ContentError(f"Source returned HTTP {response.status_code}. Check its access and configuration.")
    return response.json()


def analytics_response(response):
    if response.status_code == 400:
        try:
            error = response.json().get("error", {})
            if error.get("status") == "INVALID_ARGUMENT":
                raise ContentError("GA4 rejected the report dimensions, metrics, or filters (INVALID_ARGUMENT).")
        except (ValueError, AttributeError, TypeError):
            pass
    if response.status_code == 403:
        try:
            error = response.json()["error"]
            reasons = [detail.get("reason") for detail in error.get("details", [])]
            if "SERVICE_DISABLED" in reasons:
                raise ContentError("GA4 Data API is disabled for the credential's Cloud project. Enable analyticsdata.googleapis.com.")
            if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons:
                raise ContentError("The Google credential lacks the analytics.readonly OAuth scope.")
            if "sufficient permissions for this property" in error.get("message", "").lower():
                raise ContentError("The Google credential lacks access to the configured GA4 property. Check its Viewer grant and property ID.")
        except (ValueError, KeyError, AttributeError, TypeError):
            pass
    return response_data(response)


def shopify_token(client, shop, client_id, client_secret):
    return response_data(client.post(
        f"https://{shop}/admin/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    ))["access_token"]


class StorefrontHead(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.title = []
        self.in_title = False
        self.finished = False

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.finished = True
        if self.finished:
            return
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = attrs.get("property") or attrs.get("name")
            field = {"description": "meta_description", "og:title": "social_title",
                     "og:description": "social_description"}.get(name)
            if field and attrs.get("content"):
                self.fields.setdefault(field, attrs["content"].strip())
        elif tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            if attrs.get("href"):
                self.fields.setdefault("canonical_url", attrs["href"].strip())

    def handle_endtag(self, tag):
        if tag == "head":
            self.finished = True
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title and not self.finished:
            self.title.append(data)


def storefront_metadata(client, url):
    head = StorefrontHead()
    remaining = 256 * 1024
    unavailable = {"seo_status": "Could not verify the published page"}
    try:
        with client.stream("GET", url, follow_redirects=False, timeout=5) as response:
            if not response.is_success or "text/html" not in response.headers.get("content-type", "").lower():
                return unavailable
            for chunk in response.iter_text(chunk_size=8192):
                head.feed(chunk[:remaining])
                remaining -= len(chunk)
                if head.finished or remaining <= 0:
                    break
    except (httpx.HTTPError, ValueError):
        return unavailable
    title = " ".join("".join(head.title).split())
    if title:
        head.fields["seo_title"] = title
    complete = all(head.fields.get(key) for key in ("seo_title", "meta_description", "canonical_url"))
    return {**head.fields, "seo_status": "Read from published page" if complete else "Some SEO fields were unavailable",
            "seo_checked_at": datetime.now(timezone.utc).isoformat()}


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
            url = storefront.rstrip("/") + path
            yield {
                "external_id": article["id"], "title": article["title"],
                "url": url, "published_at": article["publishedAt"],
                "data": {**article, **storefront_metadata(client, url)},
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


def analytics_reports(client, property_id, url, start, end, utms, *, kinds=None):
    endpoint = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    headers = google_headers(endpoint, "analytics.readonly")
    path = urlsplit(url).path
    landing = ("sessions", "engagedSessions", "ecommercePurchases", "purchaseRevenue")
    acquisition = ("sessions", "engagedSessions", "engagementRate", "userEngagementDuration", "activeUsers", "newUsers")
    traffic_dimensions = ("landingPage", "sessionSource", "sessionMedium", "sessionDefaultChannelGroup")
    reports = [("ga4_views", "pagePath", ("screenPageViews", "activeUsers", "userEngagementDuration"), [], "", ()),
               ("ga4_landing", "landingPage", landing, [], "", ())]
    for utm in utms:
        filters = [("sessionManualSource", utm["utm_source"]), ("sessionManualMedium", utm["utm_medium"]),
                   ("sessionManualCampaignName", utm["utm_campaign"])]
        if utm.get("utm_content"):
            filters.append(("sessionManualAdContent", utm["utm_content"]))
        reports.append(("ga4_facebook", "landingPage", landing, filters, json.dumps(utm, sort_keys=True), ()))
    reports.append(("ga4_traffic", "landingPage", acquisition, [], "", traffic_dimensions))
    reports.append(("ga4_traffic", "pagePath", ("screenPageViews", "activeUsers", "userEngagementDuration"),
                    [], "", ("pagePath", *traffic_dimensions[1:])))
    for kind, dimension, metrics, extra, segment, breakdown in reports:
        if kinds is not None and kind not in kinds:
            continue
        paths = sorted({path.rstrip("/"), path.rstrip("/") + "/"})
        expressions = [{"filter": {"fieldName": dimension, "inListFilter": {"values": paths, "caseSensitive": True}}}]
        expressions.extend({"filter": {"fieldName": key, "stringFilter": {"value": value, "caseSensitive": True}}}
                           for key, value in extra)
        request = {
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": name} for name in ("date", *breakdown)],
            "metrics": [{"name": name} for name in metrics],
            "dimensionFilter": {"andGroup": {"expressions": expressions}} if extra else expressions[0],
            "limit": "10000", "offset": "0",
            "orderBys": [{"dimension": {"dimensionName": name}} for name in ("date", *breakdown)],
        }
        while True:
            result = analytics_response(client.post(endpoint, headers=headers, json=request))
            metadata = result.get("metadata", {})
            if metadata.get("subjectToThresholding") or metadata.get("dataLossFromOtherRow"):
                raise ContentError("GA4 returned a thresholded or aggregated report. Existing data was kept.")
            if metadata.get("schemaRestrictionResponse", {}).get("activeMetricRestrictions"):
                raise ContentError("GA4 restricted one or more metrics. Check access to revenue data.")
            rows = result.get("rows", [])
            for row in rows:
                day_value, *dimensions = [value["value"] for value in row["dimensionValues"]]
                day = datetime.strptime(day_value, "%Y%m%d").date()
                values = {key: float(item["value"]) for key, item in zip(metrics, row["metricValues"], strict=True)}
                values_by_dimension = dict(zip(breakdown, dimensions, strict=True))
                if breakdown:
                    values_by_dimension["traffic_scope"] = "article_views" if dimension == "pagePath" else "landing_sessions"
                key = json.dumps(values_by_dimension, sort_keys=True) if breakdown else segment
                yield metric_row(kind, day, metadata["timeZone"], values,
                                 currency=metadata.get("currencyCode"), segment=key)
            offset = int(request["offset"]) + len(rows)
            if not rows or offset >= result.get("rowCount", offset):
                break
            request["offset"] = str(offset)


def search_article_url(site, url, canonical=None):
    def parts(candidate):
        try:
            return urlsplit(candidate or "")
        except ValueError:
            return urlsplit("")

    original, declared = parts(url), parts(canonical)
    property_url = parts(site)
    if site.startswith("sc-domain:"):
        domain = site.removeprefix("sc-domain:").lower()
        in_property = declared.hostname == domain or (declared.hostname or "").endswith("." + domain)
    else:
        in_property = (declared.scheme == property_url.scheme and declared.netloc == property_url.netloc
                       and declared.path.startswith(property_url.path or "/"))
    if (in_property and declared.scheme in ("http", "https") and not declared.username and not declared.password
            and declared.path.rstrip("/") == original.path.rstrip("/") and not declared.query and not declared.fragment):
        return declared.geturl()
    return original._replace(query="", fragment="").geturl()


def inspect_article(client, site, url):
    endpoint = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
    now = datetime.now(timezone.utc).isoformat()
    manual = "https://search.google.com/search-console/inspect?resource_id=" + quote(site, safe="")
    result = {"checked_at": now, "inspected_url": url, "manual_url": manual}
    try:
        response = client.post(endpoint, headers=google_headers(endpoint, "webmasters.readonly"),
                               json={"siteUrl": site, "inspectionUrl": url, "languageCode": "en"})
        if not response.is_success:
            status = {401: "Authentication failed", 403: "Permission unavailable"}.get(
                response.status_code, "API request failure")
            if response.status_code == 403:
                try:
                    details = response.json().get("error", {}).get("details", [])
                    if any(detail.get("reason") == "SERVICE_DISABLED" for detail in details):
                        status = "Search Console API disabled in the credential project"
                except (ValueError, AttributeError, TypeError):
                    pass
            return {**result, "status": status, "http_status": response.status_code}
        inspection = response.json()["inspectionResult"]
        fields = inspection.get("indexStatusResult", {})
    except (ContentError, httpx.HTTPError, KeyError, TypeError, ValueError):
        return {**result, "status": "API request failure"}
    verdict = fields.get("verdict")
    status = {"PASS": "Indexed", "FAIL": "Not indexed", "NEUTRAL": "Not indexed"}.get(verdict, "Unavailable")
    return {**result, "status": status, "inspection_url": inspection.get("inspectionResultLink"),
            **{key: fields[key] for key in ("verdict", "coverageState", "indexingState", "lastCrawlTime",
                                           "googleCanonical", "userCanonical", "pageFetchState", "robotsTxtState")
               if key in fields}}


def search_reports(client, site, url, start, end):
    endpoint = f"https://www.googleapis.com/webmasters/v3/sites/{quote(site, safe='')}/searchAnalytics/query"
    headers = google_headers(endpoint, "webmasters.readonly")
    for kind, dimensions in (("gsc_page", ["date"]), ("gsc_query", ["date", "query"])):
        offset = 0
        while True:
            result = response_data(client.post(endpoint, headers=headers, json={
                "startDate": start.isoformat(), "endDate": end.isoformat(), "type": "web",
                "dataState": "final", "aggregationType": "auto", "dimensions": dimensions,
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


def search_property_sample(client, site, start, end):
    endpoint = f"https://www.googleapis.com/webmasters/v3/sites/{quote(site, safe='')}/searchAnalytics/query"
    data = response_data(client.post(endpoint, headers=google_headers(endpoint, "webmasters.readonly"), json={
        "startDate": start.isoformat(), "endDate": end.isoformat(), "type": "web", "dataState": "final",
        "dimensions": ["page"], "aggregationType": "auto", "rowLimit": 1,
    }))
    return next(iter(data.get("rows", [])), None)


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


def post_hashtags(message):
    text = re.sub(r"https?://\S+", "", message)
    tags = {}
    for tag in re.findall(r"(?<![\w#])#\w+", text):
        tags.setdefault(tag.casefold(), tag)
    return list(tags.values())


def facebook_video_reference(url):
    parsed = urlsplit(destination(url) or "")
    if parsed.hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        return None, None
    match = re.search(r"/(reel|reels|videos)/(\d+)(?:/|$)", parsed.path)
    if match:
        return ("reel" if match[1] in {"reel", "reels"} else "video"), match[2]
    video_id = parse_qs(parsed.query).get("v", [""])[0]
    if parsed.path in ("/watch", "/watch/", "/video.php") and re.fullmatch(r"[0-9]+", video_id):
        return "video", video_id
    return None, None


def facebook_posts(client, page, token, version):
    endpoint = f"https://graph.facebook.com/{version}/{page}/published_posts"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"fields": "id,message,created_time,permalink_url,"
                        "attachments{url,target,type,media_type,media,subattachments{url,target}}",
              "limit": 50}
    while True:
        body = response_data(client.get(endpoint, headers=headers, params=params))
        for post in body.get("data", []):
            message = post.get("message") or ""
            links = {url for match in re.findall(r'https?://[^\s<>"\']+', message)
                     if (url := destination(match.rstrip(".,);]")))}
            attachments = post.get("attachments", {}).get("data", [])
            for attachment in attachments:
                for item in [attachment, *attachment.get("subattachments", {}).get("data", [])]:
                    for candidate in (item.get("url"), (item.get("target") or {}).get("url")):
                        url = destination(candidate)
                        if url:
                            links.add(url)
            title = message.strip().splitlines()
            first = next(iter(attachments), {})
            permalink = destination(post.get("permalink_url"))
            attachment_type = (first.get("media_type") or first.get("type") or "").lower()
            reference_kind, video_id = facebook_video_reference(permalink)
            if not reference_kind:
                reference_kind, video_id = facebook_video_reference(first.get("url"))
            if not reference_kind:
                reference_kind, video_id = facebook_video_reference((first.get("target") or {}).get("url"))
            if reference_kind:
                kind = reference_kind
            elif "video" in attachment_type:
                kind = "video"
            elif attachment_type in {"photo", "image", "album"}:
                kind = "photo"
            elif links:
                kind = "link"
            else:
                kind = "text" if message and not attachments else "unknown"
            target_id = str((first.get("target") or {}).get("id") or "")
            yield {"external_id": post["id"], "title": title[0][:180] if title else "Facebook post",
                   "url": permalink, "published_at": post["created_time"],
                   "data": {"links": sorted(links), "message": message, "hashtags": post_hashtags(message),
                            "content_type": kind,
                            "video_id": video_id or (target_id if kind in {"reel", "video"} and target_id.isdecimal() else None),
                            "media_reference": destination(((first.get("media") or {}).get("image") or {}).get("src"))}}
        paging = body.get("paging", {})
        if not paging.get("next"):
            return
        params["after"] = paging["cursors"]["after"]


def facebook_reader(client, token, states):
    headers = {"Authorization": f"Bearer {token}"}
    halted = None

    def read(url, params, key):
        nonlocal halted
        if halted:
            states[key] = halted
            return {}
        try:
            response = client.get(url, headers=headers, params=params)
        except httpx.HTTPError:
            states[key] = "API request failure"
            return {}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if not response.is_success or body.get("error"):
            code = body.get("error", {}).get("code")
            if response.status_code == 401 or code == 190:
                states[key] = "Authentication failed"
                halted = states[key]
            elif response.status_code == 429 or code in (4, 17, 32, 613, 80001):
                states[key] = "Rate limited"
                halted = states[key]
            elif response.status_code == 403 or code in (10, 200):
                states[key] = "Permission unavailable"
            elif code == 100:
                text = str(body.get("error", {}).get("message", "")).lower()
                states[key] = "Unsupported metric" if "metric" in text else "Unsupported or inaccessible for this content"
            else:
                states[key] = "API request failure"
            return {}
        states[key] = "No data returned"
        return body
    return read


def facebook_metrics(client, post, token, version, *, availability=None, video_id=None):
    endpoint = f"https://graph.facebook.com/{version}/{post}"
    states = availability if availability is not None else {}
    results = {}
    read = facebook_reader(client, token, states)

    for field in ("reactions", "comments", "shares"):
        expression = field if field == "shares" else f"{field}.limit(0).summary(true)"
        data = read(endpoint, {"fields": expression}, field).get(field)
        if data is not None:
            count = data.get("count") if field == "shares" else data.get("summary", {}).get("total_count")
            if isinstance(count, int):
                results[field] = count
                states[field] = "Reported"
    requests = [(f"{endpoint}/insights", metric) for metric in META_METRICS]
    if video_id:
        requests.extend((f"https://graph.facebook.com/{version}/{video_id}/video_insights", metric)
                        for metric in META_VIDEO_METRICS)
    for url, metric in requests:
        params = {"metric": metric}
        if url.endswith("/insights"):
            params["period"] = "lifetime"
        for entry in read(url, params, metric).get("data", []):
            values = entry.get("values", [])
            value = entry.get("total_value", {}).get("value", values[-1].get("value") if values else None)
            if value is not None:
                results[metric] = value
                states[metric] = "Reported"
    if "reactions" not in results and states.get("reactions") != "Authentication failed":
        metric = "post_reactions_by_type_total"
        for entry in read(f"{endpoint}/insights", {"metric": metric, "period": "lifetime"}, metric).get("data", []):
            values = entry.get("values", [])
            counts = entry.get("total_value", {}).get("value", values[-1].get("value") if values else None)
            if isinstance(counts, dict) and counts and all(type(value) is int and value >= 0 for value in counts.values()):
                results["reactions"] = sum(counts.values())
                states["reactions"] = "Reported via reactions by type"
    return results
