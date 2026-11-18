from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from backend import auth, main, chatgpt_ads
from backend.connectors import chatgpt_ads as connector
from backend.channels import classify
from frontend.chatgpt_ads import totals, add_report
from frontend.reports import Report


def config():
    return {"key": "secret-fixture", "account_id": "adacct_123", "click_window": 30, "view_window": 1}


def day():
    return str(datetime.now(ZoneInfo("America/Toronto")).date() - timedelta(days=1))


def test_paginated_read_only_import_preserves_zero_and_missing():
    requests = []
    def transport(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer secret-fixture"
        assert "secret-fixture" not in str(request.url)
        if request.url.path.endswith("/ad_account"):
            return httpx.Response(200, json={"id": "adacct_123", "timezone": "America/Toronto", "currency_code": "CAD"})
        query = request.url.params
        assert query["aggregation_level"] == "ad"
        assert query["time_granularity"] == "daily"
        assert query["attribution_time_basis"] == "ad_event_time"
        assert query["attribution_window_days"] == "30" and query["view_through_attribution_window_days"] == "1"
        assert json.loads(query["time_ranges[]"])["until"] == day()
        second = "after" in query
        row = {"readable_time": day(), "ad_id": "ad_2" if second else "ad_1", "campaign_id": "cmpn_1",
               "spend": "0" if second else "10.50", "impressions": 2, "clicks": 0, "conversions": None,
               "order_created_attributed_sales": None}
        return httpx.Response(200, json={"data": [row], "has_more": not second, "last_id": "cursor-two"})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        data = connector.fetch(client, config(), 30)
    assert len(data["daily"]) == 2
    assert totals(data["daily"])["spend"] == 10.5
    assert totals(data["daily"])["clicks"] == 0
    assert totals(data["daily"])["conversions"] is None
    assert totals(data["daily"])["calculated_cpc"] is None
    assert totals([])["spend"] is None


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500])
def test_errors_never_expose_key_or_upstream_body(code):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(code, json={"error": "secret-fixture"}))) as client:
        with pytest.raises(connector.AdsError) as exc:
            connector.get(client, config(), "/ad_account")
    assert "secret-fixture" not in str(exc.value)


def test_account_mismatch_and_duplicate_rows_fail():
    for duplicate in (False, True):
        def transport(request):
            if request.url.path.endswith("/ad_account"):
                return httpx.Response(200, json={"id": "adacct_123" if duplicate else "wrong", "timezone": "America/Toronto", "currency_code": "CAD"})
            row = {"readable_time": day(), "ad_id": "ad_1", "campaign_id": "cmpn_1"}
            return httpx.Response(200, json={"data": [row, row], "has_more": False})
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            with pytest.raises(connector.AdsError, match="duplicate|different ad account"):
                connector.fetch(client, config(), 30)


def test_config_is_tenant_scoped(monkeypatch):
    monkeypatch.setenv("OPENAI_ADS_ORGANIZATION_ID", "one")
    monkeypatch.setenv("OPENAI_ADS_API_KEY", "secret-fixture")
    assert connector.configuration("two") is None
    assert connector.configuration("one")["key"] == "secret-fixture"
    monkeypatch.setenv("OPENAI_ADS_VIEW_WINDOW_DAYS", "5")
    with pytest.raises(connector.AdsError):
        connector.configuration("one")


def test_sync_failure_preserves_existing_report(monkeypatch):
    statements = []
    class Cursor:
        def execute(self, sql, params=None):
            statements.append(sql)
        def fetchone(self):
            return {"acquired": True}
    @contextmanager
    def database():
        yield Cursor()
    monkeypatch.setattr(chatgpt_ads, "database", database)
    monkeypatch.setattr(chatgpt_ads, "configuration", lambda org: config())
    monkeypatch.setattr(chatgpt_ads, "fetch", lambda *args: (_ for _ in ()).throw(connector.AdsError("API failure")))
    with pytest.raises(connector.AdsError):
        chatgpt_ads.run_sync("one")
    assert not any("insert into marketing_chatgpt_ads_reports" in sql for sql in statements)
    assert any("sync_runs" in sql for sql in statements)


def test_campaign_export_filter_and_referral_classification():
    data = {"daily": [{"campaign_id": "keep", "spend": 5}, {"campaign_id": "exclude", "spend": 100}],
            "coverage": True, "stale": False, "policy": "Fixture", "attribution": {"click_window_days": 30}}
    report = Report()
    add_report(report, data, ["keep"])
    sections = dict(report.sections)
    assert sections["ChatGPT Ads overview"][0]["spend"] == 5
    assert len(sections["ChatGPT Ads daily ad performance"]) == 1
    assert report.export()[1] == "md"
    assert classify({"source": "chatgpt.com", "medium": "referral"})["channel"] == "ChatGPT Referral — Unattributed"
    assert classify({"source": "chatgpt", "medium": "cpc"})["channel"] == "ChatGPT Paid"


def test_routes_require_membership_admin_and_valid_dates(monkeypatch):
    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.setattr(auth, "MODE", "live")
    monkeypatch.setattr(main.repo, "memberships", lambda user: [{"id": "one", "role": "member"}])
    monkeypatch.setattr(auth, "auth_call", lambda *args, **kwargs: {"id": "6fab9887-3954-4dce-83e2-3c445024cda1"})
    monkeypatch.setattr(chatgpt_ads, "status", lambda org: {"configured": True, "accounts": [], "runs": []})
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer fixture"}
    assert client.get("/orgs/one/chatgpt-ads/status").status_code == 401
    assert client.get("/orgs/two/chatgpt-ads/status", headers=headers).status_code == 404
    assert client.get("/orgs/one/chatgpt-ads/status", headers=headers).status_code == 200
    assert client.post("/orgs/one/chatgpt-ads/sync", headers=headers).status_code == 403
    assert client.get("/orgs/one/chatgpt-ads/report", params={"start": "2026-11-15", "end": "2026-11-01"}, headers=headers).status_code == 422
