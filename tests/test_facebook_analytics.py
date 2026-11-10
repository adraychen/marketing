import os
from contextlib import contextmanager
from datetime import date
from uuid import uuid4

import httpx
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from pydantic import ValidationError

from backend import content_repository, facebook_analytics
from frontend import reports
from frontend.facebook_analytics import manual_rows, page_rows

DAY = date(2026, 11, 9)


def test_page_metrics_keep_zero_and_provider_reporting_day():
    def respond(request):
        metric = request.url.params.get("metric")
        if request.url.params.get("fields") == "followers_count":
            return httpx.Response(200, json={"followers_count": 0})
        if metric == "page_media_view":
            return httpx.Response(200, json={"data": [{"name": metric, "period": "day", "values": [
                {"end_time": "2026-11-09T08:00:00+0000", "value": 0},
                {"end_time": "2026-11-10T08:00:00+0000", "value": 5},
            ]}]})
        return httpx.Response(403, json={"error": {"code": 200, "message": "fixture secret"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        rows = facebook_analytics.collect_page_metrics(
            client, "123", "fixture-token", "v26.0", date(2026, 11, 8), DAY, DAY)
    assert ("followers_count", DAY, "point", 0, None, None, "Reported") in rows
    values = [row for row in rows if row[0] == "page_media_view" and row[2] == "day"]
    assert [(row[1].isoformat(), row[3]) for row in values] == [("2026-11-08", 0), ("2026-11-09", 5)]
    assert {row[5] for row in values} == {"-08:00"}
    assert any(row[0] == "page_video_views" and row[-1] == "Permission unavailable" for row in rows)
    assert "fixture secret" not in repr(rows)


def test_page_probe_halts_after_quota_failure():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        rows = facebook_analytics.collect_page_metrics(client, "123", "fixture", "v26.0", DAY, DAY, DAY)
    assert len(calls) == 1
    assert {row[-1] for row in rows} == {"Rate limited"}


def daily(metric, day, value, basis="day"):
    return {"page_id": "123", "metric": metric, "day": day, "basis": basis, "value": value,
            "api_version": "v26.0", "reporting_offset": "+00:00", "collected_at": "2026-11-10T10:00:00Z"}


def observation(**changes):
    return {"id": "example", "page_id": "123", "item_id": None, "metric": "page_media_view", "basis": "period",
            "period_start": "2026-11-08", "period_end": "2026-11-09", "observed_day": "2026-11-09",
            "value": 15, "unit": "views", "reporting_offset": "+00:00", "notes": "", **changes}


def test_full_period_only_and_manual_discrepancy_are_exported(monkeypatch):
    first, last = "2026-11-08", "2026-11-09"
    data = {"page": [
        daily("page_media_view", first, 5), daily("page_media_view", last, 9),
        daily("page_total_media_view_unique", first, 2), daily("page_total_media_view_unique", last, 3),
        daily("followers_count", last, 0, "point"),
        daily("page_daily_follows_unique", last, 3), daily("page_daily_unfollows_unique", last, 1),
    ], "manual": [observation(), observation(reporting_offset="-08:00")], "pages": ["123"], "availability": []}
    rows = page_rows(data, first, last)
    assert [row["metric"] for row in rows if row["basis"] == "period"] == ["page_media_view"]
    assert next(row["value"] for row in rows if row["metric"] == "net_follows") == 2
    manual = manual_rows(data, rows, {})
    assert manual[0]["api_value"] == 14 and manual[0]["manual_minus_api"] == 1
    assert manual[1]["api_value"] is None
    monkeypatch.setattr(reports, "call", lambda *args, **kwargs: data)
    report = reports.Report()
    report.table("Google Ads overview", [{"spend": 10}])
    reports.add_facebook_analytics(report, "/content", {"start": first, "end": last}, {}, "")
    markdown, extension, _ = report.export()
    assert extension == "md"
    for label in (b"Google Ads overview", b"Facebook Page Visibility", b"Facebook Audience Growth",
                  b"Manual Analytics Observations", b"Manual / Facebook Business Dashboard", b"manual_minus_api"):
        assert label in markdown


def test_manual_input_validates_scope_period_and_signed_net():
    row = facebook_analytics.ManualObservation.model_validate(observation(metric="net_follows", value=-1))
    assert row.value == -1
    for invalid in (observation(value=-1), observation(metric="comments"), observation(period_end="2026-11-10"),
                    observation(metric="followers_count")):
        with pytest.raises(ValidationError):
            facebook_analytics.ManualObservation.model_validate(invalid)


@pytest.mark.skipif(not os.getenv("TEST_CONTENT_DATABASE_URL"), reason="requires the Facebook analytics migration")
def test_manual_create_duplicate_edit_delete_and_tenant_isolation(monkeypatch):
    conn = psycopg2.connect(os.environ["TEST_CONTENT_DATABASE_URL"])
    org, other = str(uuid4()), str(uuid4())
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            @contextmanager
            def database():
                yield cur

            monkeypatch.setattr(facebook_analytics, "database", database)
            cur.execute("insert into marketing_organizations (id,name) values (%s,'Test'),(%s,'Other')", (org, other))
            content_repository.write_items(cur, org, "facebook", "123", [
                {"external_id": "fixture", "title": "Fixture", "url": None, "published_at": DAY, "data": {}},
            ])
            row = facebook_analytics.ManualObservation.model_validate(observation())
            saved = str(facebook_analytics.save_manual(org, row)["id"])
            assert facebook_analytics.save_manual(org, row) is None
            assert not facebook_analytics.delete_manual(other, saved)
            assert facebook_analytics.save_manual(org, row.model_copy(update={"value": 20}), saved)
            assert facebook_analytics.analytics(org, DAY, DAY)["manual"][0]["value"] == 20
            assert facebook_analytics.delete_manual(org, saved)
            assert not facebook_analytics.analytics(org, DAY, DAY)["manual"]
    finally:
        conn.rollback()
        conn.close()
