import json
import os
from datetime import date
from uuid import uuid4

import httpx
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from backend import content_repository as repo
from backend.connectors import content
from frontend.visibility import facebook_changes, facebook_latest, prepublication_rows, search_queries, search_totals

DAY = date(2026, 11, 6)
ARTICLE = "https://okoostore.com/blogs/journal/citizen-eco-drive-vs-automatic"
TAGGED = ARTICLE + "?utm_source=facebook&utm_medium=organic_social&utm_campaign=" + repo.PILOT


@pytest.mark.parametrize(("error", "hint"), [
    ({"details": [{"reason": "SERVICE_DISABLED", "metadata": {"private": "secret"}}]}, "Data API is disabled"),
    ({"message": "User does not have sufficient permissions for this property. secret"}, "lacks access"),
])
def test_ga4_denial_logs_safe_hint(error, hint):
    with pytest.raises(content.ContentError, match=hint) as exc:
        content.analytics_response(httpx.Response(403, json={"error": error}))
    assert "secret" not in str(exc.value)


def test_ga4_requests_separate_page_landing_and_facebook_reports(monkeypatch):
    monkeypatch.setattr(content, "google_headers", lambda *_args: {})
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            "metadata": {"timeZone": "America/Toronto", "currencyCode": "CAD"},
            "rows": [{"dimensionValues": [{"value": "20261106" if dimension["name"] == "date" else dimension["name"]}
                                         for dimension in body["dimensions"]],
                      "metricValues": [{"value": "3"} for _ in body["metrics"]]}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        rows = list(content.analytics_reports(client, "123", ARTICLE, DAY, DAY, [repo.link_utm(TAGGED)]))
    assert [row["report"] for row in rows] == ["ga4_views", "ga4_landing", "ga4_facebook", "ga4_traffic"]
    assert requests[0]["dimensionFilter"]["filter"]["fieldName"] == "pagePath"
    filters = requests[2]["dimensionFilter"]["andGroup"]["expressions"]
    assert [item["filter"]["fieldName"] for item in filters] == [
        "landingPage", "sessionManualSource", "sessionManualMedium", "sessionManualCampaignName",
    ]
    assert rows[2]["currency"] == "CAD"
    assert json.loads(rows[2]["segment"]) == repo.PILOT_UTM
    assert json.loads(rows[3]["segment"])["sessionMedium"] == "sessionMedium"


def test_ga4_thresholded_results_fail_before_replacing_data(monkeypatch):
    monkeypatch.setattr(content, "google_headers", lambda *_args: {})
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"metadata": {"subjectToThresholding": True}}))) as client:
        with pytest.raises(content.ContentError, match="thresholded"):
            list(content.analytics_reports(client, "123", ARTICLE, DAY, DAY, []))


def test_meta_probes_preserve_zero_and_skip_retired_or_unavailable_fields():
    def respond(request):
        metric = request.url.params.get("metric")
        if metric == "post_media_view":
            return httpx.Response(200, json={"data": [{"values": [{"value": {"organic": 24}}]}]})
        if metric == "post_total_media_view_unique":
            return httpx.Response(200, json={"data": [{"total_value": {"value": 13}}]})
        if metric == "post_clicks":
            return httpx.Response(200, json={"data": [{"values": [{"value": 0}]}]})
        return httpx.Response(400, json={"error": {"code": 100}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        metrics = content.facebook_metrics(client, "123_456", "fake", "v26.0")
    assert metrics == {"post_media_view": {"organic": 24}, "post_total_media_view_unique": 13, "post_clicks": 0}
    assert not any("impressions" in key for key in metrics)


def test_discovered_link_keeps_original_utm_and_canonical_path():
    url = "https://l.facebook.com/l.php?u=" + httpx.QueryParams({"url": TAGGED}).__str__().removeprefix("url=")
    assert content.destination(url) == TAGGED
    assert repo.url_key(TAGGED) == repo.url_key(ARTICLE)
    assert repo.link_utm(TAGGED) == repo.PILOT_UTM


def test_search_uses_article_canonical_and_separate_dimensions(monkeypatch):
    monkeypatch.setattr(content, "google_headers", lambda *_args: {})
    site = "https://okoostore.com/"
    canonical = ARTICLE + "/"
    assert content.search_article_url(site, ARTICLE, canonical) == canonical
    assert content.search_article_url(site, ARTICLE, "https://example.org" + canonical.split(".com")[1]) == ARTICLE
    assert content.search_article_url(site, ARTICLE, site + "blogs/journal/other") == ARTICLE
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        keys = [str(DAY)] + (["citizen"] if "query" in body["dimensions"] else [])
        return httpx.Response(200, json={"rows": [
            {"keys": keys, "impressions": 5, "clicks": 0, "position": 4, "ctr": 0},
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        rows = list(content.search_reports(client, site, canonical, DAY, DAY))
    assert [body["dimensions"] for body in requests] == [["date"], ["date", "query"]]
    assert requests[0]["dimensionFilterGroups"][0]["filters"][0]["expression"] == canonical
    assert rows[0]["metrics"]["clicks"] == 0 and rows[1]["segment"] == "citizen"
    rows[1]["source_account"] = site
    extra = {**rows[1], "metrics": {"impressions": 15, "clicks": 1, "position": 8}}
    assert search_queries([rows[1], extra])[0]["position"] == 7
    assert search_totals([])["impressions"] is None


def test_url_inspection_permission_and_indexing(monkeypatch):
    monkeypatch.setattr(content, "google_headers", lambda *_args: {})
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        result = content.inspect_article(client, "https://okoostore.com/", ARTICLE)
    assert result["status"] == "Permission unavailable" and result["manual_url"].startswith("https://search.google.com/")
    payload = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS", "googleCanonical": ARTICLE}}}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        result = content.inspect_article(client, "https://okoostore.com/", ARTICLE)
    assert result["status"] == "Indexed" and result["googleCanonical"] == ARTICLE


def test_prepublication_uses_ga4_reporting_timezone():
    item = {"published_at": "2026-11-07T03:58:42+00:00"}
    rows = [{"report": "ga4_views", "day": day, "timezone": "America/Toronto", "metrics": {"screenPageViews": 1}}
            for day in ("2026-11-05", "2026-11-06")]
    assert [row["day"] for row in prepublication_rows(item, rows)] == ["2026-11-05"]


def test_meta_video_endpoint_and_lifetime_corrections():
    def respond(request):
        if request.url.path.endswith("/42/video_insights") and request.url.params.get("metric") == "total_video_views":
            return httpx.Response(200, json={"data": [{"values": [{"value": 0}]}]})
        if request.url.params.get("metric") == "post_media_view":
            return httpx.Response(403, json={"error": {"code": 200}})
        return httpx.Response(400, json={"error": {"code": 100}})

    states = {}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        metrics = content.facebook_metrics(client, "1_2", "fake", "v26.0", availability=states, video_id="42")
    snapshot = {"api_version": "v26.0", "observed_at": "2026-11-07T00:00:00Z",
                "metrics": {**metrics, "availability": states}}
    latest = facebook_latest([snapshot], "reel")
    assert latest["total_video_views"] == 0
    assert latest["availability"]["post_media_view"] == "Permission unavailable"
    earlier = {**snapshot, "observed_at": "2026-11-06T00:00:00Z", "metrics": {"total_video_views": 3}}
    assert facebook_changes([snapshot, earlier])[0]["change"] is None


def test_markdown_visibility_preserves_google_ads_and_explains_missing_rows(monkeypatch):
    from frontend import reports

    catalog = {"items": [{"id": "article", "source": "shopify", "title": "Citizen article",
                          "url": ARTICLE, "published_at": "2026-11-07T03:58:42+00:00"}],
               "campaigns": [], "sources": []}
    details = {"daily": [], "links": [], "snapshots": [],
               "metadata": {"seo_title": "Citizen watches", "indexing": {"status": "Indexed"}}}
    monkeypatch.setattr(reports, "call", lambda *args, **kwargs: details)
    report = reports.Report()
    report.table("Google Ads overview", [{"spend": 4}])
    reports.add_content(report, "/content", catalog, "", {"start": "2026-11-01", "end": "2026-11-08"})
    data, extension, _ = report.export()
    assert extension == "md"
    assert b"Google Ads overview" in data
    assert b"Google Search Console article performance" in data
    assert b"Citizen watches" in data and b"Indexed" in data
    assert b"does not establish zero impressions" in data
    assert b"Google Search queries by article" not in data


@pytest.mark.skipif(not os.getenv("TEST_CONTENT_DATABASE_URL"), reason="requires the content marketing migration")
def test_content_schema_pilot_associations_and_isolation(monkeypatch):
    from contextlib import contextmanager

    conn = psycopg2.connect(os.environ["TEST_CONTENT_DATABASE_URL"])
    org, other, user = (str(uuid4()) for _ in range(3))
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            @contextmanager
            def test_database():
                yield cur

            monkeypatch.setattr(repo, "database", test_database)
            cur.execute("insert into marketing_organizations (id,name) values (%s,'Content Test'),(%s,'Other')",
                        (org, other))
            cur.execute("insert into auth.users(id) values (%s)", (user,))
            cur.execute("insert into marketing_memberships (organization_id,user_id) values (%s,%s)", (org, user))
            article = {"external_id": "article", "url": ARTICLE, "title": "Article", "published_at": DAY, "data": {}}
            post = {"external_id": "post", "url": None, "title": "Post", "published_at": DAY, "data": {"links": [TAGGED]}}
            for _ in range(2):
                repo.write_items(cur, org, "shopify", "shop", [article])
                repo.write_items(cur, org, "facebook", "page", [post])
                repo.associate(cur, org, "shop", "page")
            repo.write_items(cur, other, "shopify", "shop", [article])
            catalog = repo.catalog(org)
            assert len(catalog["items"]) == 2
            assert all(item["campaigns"] == [repo.PILOT] for item in catalog["items"])
            article_id = next(item["id"] for item in catalog["items"] if item["source"] == "shopify")
            for clicks in (5, 7):
                row = content.metric_row("gsc_page", DAY, "America/Los_Angeles", {"clicks": clicks})
                repo.write_daily(cur, org, article_id, "site", ["gsc_page"], DAY, DAY, [row])
            details = repo.details(org, article_id, DAY, DAY)
            assert details["links"][0]["utm"] == repo.PILOT_UTM
            assert [row["metrics"]["clicks"] for row in details["daily"]] == [7]
            post_id = next(item["id"] for item in catalog["items"] if item["source"] == "facebook")
            for views in (0, 0, 2):
                repo.write_snapshot(cur, org, post_id, DAY, "v26.0", {"post_media_view": views})
            assert [row["metrics"]["post_media_view"] for row in repo.details(org, post_id, DAY, DAY)["snapshots"]] == [0, 2]
            assert repo.details(other, article_id, DAY, DAY) is None
            cur.execute("set local role authenticated")
            cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (user,))
            cur.execute("select count(*) as count from marketing_content_items where organization_id in (%s,%s)", (org, other))
            assert cur.fetchone()["count"] == 2
    finally:
        conn.rollback()
        conn.close()
