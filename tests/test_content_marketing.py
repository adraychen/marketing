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

DAY = date(2026, 11, 6)
ARTICLE = "https://okoostore.com/blogs/journal/citizen-eco-drive-vs-automatic"
TAGGED = ARTICLE + "?utm_source=facebook&utm_medium=organic_social&utm_campaign=" + repo.PILOT


def test_ga4_requests_separate_page_landing_and_facebook_reports(monkeypatch):
    monkeypatch.setattr(content, "google_headers", lambda *_args: {})
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            "metadata": {"timeZone": "America/Toronto", "currencyCode": "CAD"},
            "rows": [{"dimensionValues": [{"value": "20261106"}],
                      "metricValues": [{"value": "3"} for _ in body["metrics"]]}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        rows = list(content.analytics_reports(client, "123", ARTICLE, DAY, DAY, [repo.link_utm(TAGGED)]))
    assert [row["report"] for row in rows] == ["ga4_views", "ga4_landing", "ga4_facebook"]
    assert requests[0]["dimensionFilter"]["filter"]["fieldName"] == "pagePath"
    filters = requests[2]["dimensionFilter"]["andGroup"]["expressions"]
    assert [item["filter"]["fieldName"] for item in filters] == [
        "landingPage", "sessionManualSource", "sessionManualMedium", "sessionManualCampaignName",
    ]
    assert rows[2]["currency"] == "CAD"
    assert json.loads(rows[2]["segment"]) == repo.PILOT_UTM


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
            assert repo.details(other, article_id, DAY, DAY) is None
            cur.execute("set local role authenticated")
            cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (user,))
            cur.execute("select count(*) as count from marketing_content_items where organization_id in (%s,%s)", (org, other))
            assert cur.fetchone()["count"] == 2
    finally:
        conn.rollback()
        conn.close()
