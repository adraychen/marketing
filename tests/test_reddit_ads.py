import json
from contextlib import contextmanager
from datetime import date, datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from backend.connectors import reddit_ads as api
from backend import reddit_ads as repo, reddit_auth
from backend.reddit_metrics import totals
from frontend.reddit_ads import add_report, summary_rows
from frontend.reports import Report

ACCOUNT = {"id": "a2_one", "name": "Okoo", "currency": "CAD", "attribution_type": "ALL_CONVERSION",
           "click_attribution_window": "MONTH", "view_attribution_window": "DAY"}


def raw(ad="ad1", day="2026-11-14", **extra):
    return {"ad_id": ad, "date": day, "spend": 102280000, "impressions": 1000, "clicks": 20, "currency": "CAD",
            "conversion_purchase_clicks": 1, "conversion_purchase_views": 0, "conversion_purchase_total_value": 31450, **extra}


def observed(**extra):
    values = raw(**extra)
    metrics, availability = api.normalize(values, ACCOUNT, {"purchase"})
    return {"ad_id": values["ad_id"], "campaign_id": "c1", "ad_group_id": "g1", "day": values["date"],
            "currency": "CAD", "metrics": metrics, "availability": availability, "breakdown": "base", "segment": "",
            "attribution": {k: ACCOUNT[k] for k in ("attribution_type", "click_attribution_window", "view_attribution_window")}}


def test_units_explicit_conversion_types_and_ratios():
    row = observed()
    values = totals([row])
    assert values["spend"] == 102.28
    assert values["purchase_value"] == 314.50
    assert values["purchases"] == 1
    assert values["cpa"] == 102.28
    assert values["roas"] == pytest.approx(3.07489245)
    assert values["ctr"] == .02 and values["cpm"] == 102.28
    assert values["conversion_page_visit_clicks"] is None


def test_untracked_zero_missing_and_reported_zero():
    metric = {"currency": "CAD", "conversion_purchase_clicks": 0, "conversion_purchase_views": 0,
              "conversion_purchase_total_value": 0, "spend": 0, "clicks": 0}
    untracked, _ = api.normalize(metric, ACCOUNT, set())
    tracked, _ = api.normalize(metric, ACCOUNT, {"purchase"})
    assert untracked["purchases"] is None and untracked["purchase_value"] is None
    assert tracked["purchases"] == 0 and tracked["purchase_value"] == 0
    assert totals([{**observed(), "metrics": tracked}])["cpa"] is None
    assert totals([])["spend"] is None
    del metric["conversion_purchase_views"]
    assert api.normalize(metric, ACCOUNT, {"purchase"})[0]["purchases"] is None


@pytest.mark.parametrize("value", [True, "bad", float("nan"), -1])
def test_invalid_metrics_fail(value):
    with pytest.raises(api.RedditError):
        api.numeric(value)


def test_currency_conflict_and_different_attribution_not_combined():
    with pytest.raises(api.RedditError):
        api.normalize(raw(currency="USD"), ACCOUNT, {"purchase"})
    a = observed()
    b = {**observed(ad="ad2"), "currency": "USD"}
    assert totals([a, b])["spend"] is None
    b = {**observed(ad="ad2"), "attribution": {"attribution_type": "CLICK_THROUGH_CONVERSION"}}
    assert totals([a, b])["purchases"] is None


def test_multiple_ads_days_breakdowns_and_unique_reach():
    a, b = observed(), observed(ad="ad2", day="2026-11-15")
    a["metrics"]["reach"] = 100
    b["metrics"]["reach"] = 200
    alternative = {**a, "breakdown": "COUNTRY", "segment": "CA"}
    result = totals([a, b, alternative])
    assert result["spend"] == 204.56 and result["purchases"] == 2
    assert result["reach"] is None and result["frequency"] is None
    assert totals([a])["reach"] == 100
    assert result["cpc"] == pytest.approx(204.56 / 40)


def transport_factory(duplicate=False, optional_fail=False):
    requests = []
    def transport(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        path = request.url.path.removeprefix("/api/v3")
        if path == "/me/businesses":
            rows = [{"id": "b1"}]
        elif path == "/businesses/b1/ad_accounts":
            rows = [ACCOUNT]
        elif path.endswith("/campaigns"):
            rows = [{"id": "c1", "name": "Campaign", "effective_status": "PAUSED"}]
        elif path.endswith("/ad_groups"):
            rows = [{"id": "g1", "campaign_id": "c1", "conversion_pixel_id": "px1"}]
        elif path.endswith("/ads"):
            rows = [{"id": "ad1", "ad_group_id": "g1"}, {"id": "ad2", "ad_group_id": "g1"}]
        elif path.endswith("/pixels"):
            rows = [{"id": "px1"}]
        elif path == "/pixels/px1/last_fired_at":
            return httpx.Response(200, json={"data": {"purchase": "2026-11-01T00:00:00Z"}})
        elif path.endswith("/reports"):
            body = json.loads(request.content)["data"]
            assert body["time_zone_id"] == "UTC" and len(body["breakdowns"]) <= 3
            assert request.method == "POST"
            if len(body["breakdowns"]) > 2 and optional_fail:
                return httpx.Response(400, json={"error": "secret upstream details"})
            if "page" not in request.url.params:
                rows = [raw()]
                return httpx.Response(200, json={"data": {"metrics": rows}, "pagination": {
                    "next_url": api.BASE + "/ad_accounts/a2_one/reports?page=2"}})
            rows = [raw() if duplicate else raw(ad="ad2", day="2026-11-15")]
            return httpx.Response(200, json={"data": {"metrics": rows}, "pagination": {}})
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"data": rows, "pagination": {}})
    return transport, requests


def test_discovery_pagination_metadata_all_statuses_and_daily_import():
    transport, requests = transport_factory()
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = api.Client(http, lambda force: "test-token", "test")
        accounts = client.accounts()
        batch = api.fetch_account(client, accounts[0], date(2026, 11, 14), date(2026, 11, 15))
    assert len(batch["observations"]) == 2
    assert batch["campaigns"][0]["effective_status"] == "PAUSED"
    assert batch["observations"][1]["campaign_id"] == "c1"
    assert not any("configured_status" in str(r.url) for r in requests)
    assert totals(batch["observations"])["spend"] == 204.56


def test_duplicate_pages_fail_and_optional_breakdown_failure_is_explicit():
    transport, _ = transport_factory(duplicate=True)
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        with pytest.raises(api.RedditError, match="duplicate"):
            api.fetch_account(api.Client(http, lambda force: "test-token", "test"), ACCOUNT, date(2026,11,14), date(2026,11,15))
    transport, _ = transport_factory(optional_fail=True)
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        result = api.fetch_account(api.Client(http, lambda force: "test-token", "test"), ACCOUNT, date(2026,11,14), date(2026,11,15), ["COUNTRY"])
    assert len(result["observations"]) == 2
    assert result["limitations"] == ["COUNTRY breakdown unavailable; no partial breakdown stored."]


def test_retries_refresh_and_error_redaction():
    calls, refreshes, sleeps = [], [], []
    def transport(request):
        calls.append(request)
        status = [401, 429, 200][len(calls)-1]
        return httpx.Response(status, headers={"Retry-After": "2"}, json={"data": [], "pagination": {}, "error": "secret"})
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = api.Client(http, lambda force: refreshes.append(force) or "key", "test", sleeps.append)
        assert client.pages("/me/businesses") == []
        with pytest.raises(api.RedditError, match="unsafe"):
            client.request("GET", "https://evil.example/api/v3/me")
        with pytest.raises(api.RedditError, match="read-only"):
            client.request("POST", "/ad_accounts/a2_one/ads", {})
    assert refreshes == [False, True, False] and sleeps == [2]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="secret"))) as http:
        with pytest.raises(api.RedditError) as exc:
            api.Client(http, lambda force: "key", "test").request("GET", "/me/businesses")
        assert "secret" not in str(exc.value) and "403" in str(exc.value)


def test_history_coverage_does_not_fill_gaps():
    days = repo.merge_coverage(["2026-10-01"], date(2026,11,1), date(2026,11,2))
    assert days == ["2026-10-01", "2026-11-01", "2026-11-02"]
    assert repo.history_start(date(2026, 1, 31)) == date(2024, 2, 1)
    assert repo.history_start(date(2028, 2, 29)) == date(2026, 3, 1)


def test_write_repeated_sync_retains_older_history(monkeypatch):
    stored = {("2026-10-01", "old", "base", ""): "historical"}
    class Cursor:
        def execute(self, sql, params):
            self.sql = sql
            if sql.startswith("delete from marketing_paid_observations"):
                for key in list(stored):
                    if params[2] <= key[0] <= params[3]:
                        del stored[key]
        def fetchone(self):
            if self.sql.startswith("select id,data"):
                return None
            return {"id": "local-account"}
    def insert(cur, sql, rows):
        for row in rows:
            key = (row[3], row[4], row[7], row[8])
            stored[key] = row[-1].adapted
    monkeypatch.setattr(repo, "execute_values", insert)
    batch = {"account": ACCOUNT, "campaigns": [], "ad_groups": [], "ads": [], "observations": [observed()],
             "start": "2026-11-14", "end": "2026-11-14", "attribution": observed()["attribution"],
             "source": "Reddit Ads API v3", "known_events": ["purchase"], "limitations": []}
    for _ in range(2):
        repo.write_account(Cursor(), "org", batch)
    assert len(stored) == 2
    assert stored[("2026-10-01", "old", "base", "")] == "historical"


def test_tokens_encrypted_and_rotating_refresh_preserved(monkeypatch):
    config = {"REDDIT_ADS_TOKEN_KEY": Fernet.generate_key().decode(), "REDDIT_ADS_CLIENT_ID": "client",
              "REDDIT_ADS_CLIENT_SECRET": "secret", "REDDIT_ADS_USER_AGENT": "test"}
    saved = {}
    class Cursor:
        def execute(self, sql, params):
            if sql.startswith("insert"):
                saved["ciphertext"] = params[1]
        def fetchone(self):
            return saved
    @contextmanager
    def db():
        yield Cursor()
    reddit_auth.save(Cursor(), "org", config, {"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 0})
    assert "old-refresh" not in saved["ciphertext"]
    monkeypatch.setattr(reddit_auth, "configuration", lambda org: config)
    monkeypatch.setattr(reddit_auth, "database", db)
    monkeypatch.setattr(reddit_auth, "exchange", lambda *args: {"access_token": "new-access", "refresh_token": "rotated", "expires_at": 9999999999})
    assert reddit_auth.access_token("org") == "new-access"
    decrypted = json.loads(reddit_auth.cipher(config).decrypt(saved["ciphertext"].encode()))
    assert decrypted["refresh_token"] == "rotated"


def test_oauth_rejects_write_scopes_and_redacts_errors():
    config = {"REDDIT_ADS_CLIENT_ID": "client", "REDDIT_ADS_CLIENT_SECRET": "secret", "REDDIT_ADS_USER_AGENT": "test"}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "access_token": "secret", "scope": "adsread adsedit", "expires_in": 3600}))) as http:
        with pytest.raises(api.RedditError, match="only the adsread"):
            reddit_auth.exchange(http, config, {"grant_type": "refresh_token"})


def test_dashboard_and_export_campaign_selection():
    account = {"account": {"id": "local", "resource": "a2_one", "data": {"name": "Okoo", "currency": "CAD",
               "attribution": observed()["attribution"], "synced_at": datetime.now(timezone.utc).isoformat()}},
               "daily": [observed(), {**observed(ad="ad2"), "campaign_id": "c2"}], "breakdowns": [],
               "campaigns": [{"id": "c1"}, {"id": "c2"}], "ad_groups": [], "ads": [],
               "compatible": True, "coverage": True, "stale": False}
    data = {"accounts": [account], "start": "2026-11-14", "end": "2026-11-14", "policy": repo.POLICY}
    assert summary_rows(data)[0]["Spend"] == 204.56
    export = Report()
    add_report(export, data, ["local:c1"])
    sections = dict(export.sections)
    assert sections["Reddit Ads dashboard metrics"][0]["Spend"] == 102.28
    assert sections["Reddit Ads dashboard metrics"][0]["Conversions"] == 1
    assert len(sections["Reddit Ads daily ad performance"]) == 1
    assert export.export()[1] == "md"
    account["compatible"] = False
    assert summary_rows(data)[0]["Spend"] is None


def test_reporting_chunks_and_new_campaign_discovery_on_each_fetch():
    calls = []
    class FakeClient:
        generation = 1
        def pages(self, path, body=None):
            if body:
                calls.append(body["data"])
                return []
            if path.endswith("/campaigns"):
                return [{"id": f"c{i}"} for i in range(self.generation)]
            return []
    client = FakeClient()
    first = api.fetch_account(client, ACCOUNT, date(2026,1,1), date(2026,3,1))
    assert len(calls) == 3
    assert calls[0]["starts_at"] == "2026-01-01T00:00:00Z"
    assert calls[0]["ends_at"] == calls[1]["starts_at"]
    assert calls[-1]["ends_at"] == "2026-03-02T00:00:00Z"
    client.generation = 2
    second = api.fetch_account(client, ACCOUNT, date(2026,3,1), date(2026,3,1))
    assert len(first["campaigns"]) == 1 and len(second["campaigns"]) == 2


def test_api_guardrails_and_tenant_dependencies(monkeypatch):
    from fastapi import HTTPException
    from backend import main
    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.setattr(main, "orgs", lambda user: [{"id": "one", "role": "member"}])
    with pytest.raises(HTTPException) as exc:
        main.member("two", "user")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        main.reddit_ads_sync("one", {"role": "member"})
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        main.reddit_ads_report("one", date(2026,11,15), date(2026,11,1), {})
    assert exc.value.status_code == 422
    routes = [r for r in main.app.routes if "/reddit-ads/" in r.path]
    assert len(routes) == 3
    assert all(any(d.call is main.member for d in route.dependant.dependencies) for route in routes)


def test_failed_collection_does_not_persist_partial_batch(monkeypatch):
    statements, runs = [], []
    class Cursor:
        def execute(self, query, params=None):
            statements.append(query)
        def fetchone(self):
            return {"acquired": True}
        def fetchall(self):
            return []
    @contextmanager
    def db():
        yield Cursor()
    monkeypatch.setattr(repo, "database", db)
    monkeypatch.setattr(repo, "configuration", lambda org: {"REDDIT_ADS_USER_AGENT": "test"})
    monkeypatch.setattr(repo.Client, "accounts", lambda self: [ACCOUNT])
    monkeypatch.setattr(repo, "fetch_account", lambda *args: (_ for _ in ()).throw(api.RedditError("fixture failure")))
    monkeypatch.setattr(repo.content_repository, "sync_status", lambda *args: runs.append(args))
    with pytest.raises(api.RedditError):
        repo.run_sync("one")
    assert not any("delete from" in query or "insert into" in query for query in statements)
    assert runs[0][2] == "failed"


def test_paused_campaign_without_activity_remains_visible_and_export_event_matches():
    from frontend.reddit_ads import performance
    account = {"account": {"id": "local", "resource": "a2_one", "data": {"name": "Okoo", "currency": "CAD",
               "attribution": observed()["attribution"], "synced_at": datetime.now(timezone.utc).isoformat()}},
               "daily": [observed()], "breakdowns": [], "campaigns": [{"id": "c1"}, {"id": "paused", "effective_status": "PAUSED"}],
               "ad_groups": [], "ads": [], "compatible": True, "coverage": True, "stale": False}
    result = performance(account, "campaigns", account["daily"], campaign_ids=["c1", "paused"])
    assert len(result) == 2 and result[1]["spend"] is None
    account["daily"][0]["metrics"]["conversion_lead_clicks"] = 2
    export = Report()
    add_report(export, {"accounts": [account], "policy": repo.POLICY, "start": "2026-11-14", "end": "2026-11-14"},
               ["local:c1"], conversion="conversion_lead_clicks")
    sections = dict(export.sections)
    assert sections["Reddit Ads overview"][0]["cpa"] == 51.14
    assert sections["Reddit Ads dashboard metrics"][0]["Conversions"] == 1


def test_paid_page_renders_existing_components_and_all_daily_charts(monkeypatch):
    from frontend import reddit_ads as ui
    charts, tables = [], []
    class Panel:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def metric(self, *args): pass
        def info(self, *args): pass
        def plotly_chart(self, fig, **kwargs): charts.append(fig)
    account = {"account": {"id": "local", "resource": "a2_one", "data": {"name": "Okoo", "currency": "CAD",
               "attribution": observed()["attribution"], "synced_at": datetime.now(timezone.utc).isoformat()}},
               "daily": [observed()], "breakdowns": [], "campaigns": [{"id": "c1", "effective_status": "ACTIVE"}],
               "ad_groups": [{"id": "g1", "campaign_id": "c1"}], "ads": [{"id": "ad1", "ad_group_id": "g1"}],
               "compatible": True, "coverage": True, "stale": False}
    data = {"accounts": [account], "policy": repo.POLICY, "start": "2026-11-14", "end": "2026-11-15"}
    monkeypatch.setattr(ui, "load", lambda *args: data)
    monkeypatch.setattr(ui, "table", tables.append)
    for name in ("title", "caption", "subheader", "download_button"):
        monkeypatch.setattr(ui.st, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(ui.st, "date_input", lambda *args, **kwargs: (date(2026,11,14), date(2026,11,15)))
    monkeypatch.setattr(ui.st, "selectbox", lambda label, options, **kwargs: list(options)[0])
    monkeypatch.setattr(ui.st, "multiselect", lambda label, options, **kwargs: kwargs["default"])
    monkeypatch.setattr(ui.st, "columns", lambda count: [Panel() for _ in range(count)])
    monkeypatch.setattr(ui.st, "expander", lambda *args, **kwargs: Panel())
    ui.show("/orgs/test")
    assert len(charts) == 5
    assert tables[0][0]["purchases"] == 1
    assert len(charts[0].data[0].y) == 2  # Missing second day is a gap, not a zero.
    assert charts[0].data[0].y[0] == 102.28
