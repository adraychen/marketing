from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import auth, demo, main
from backend.connectors.google_ads import GoogleAdsConnector, GoogleAdsError, normalize_ad
from backend.jobs import import_dates
from backend.metrics import previous_period, summarize
from backend.models import Creative
from backend.validation import validate

DAY = date(2026, 11, 5)


def test_metrics_weighted_and_currency_scoped():
    rows = [
        dict(currency="CAD", spend=30, clicks=10, impressions=100, interactions=20, conversions=4, value=100),
        dict(currency="CAD", spend=20, clicks=5, impressions=50, interactions=10, conversions=1, value=50),
    ]
    result = summarize(rows, "CAD")
    assert result["ctr"] == .1
    assert result["conversion_rate"] == pytest.approx(5 / 30)
    assert result["cpc"] == pytest.approx(50 / 15)
    assert result["cpa"] == 10
    assert result["roas"] == 3
    rows[1]["value"] = None
    assert summarize(rows, "CAD")["roas"] is None
    with pytest.raises(ValueError):
        summarize(rows, "USD")
    assert summarize([], "CAD")["ctr"] is None
    assert summarize([], "CAD")["spend"] == 0
    assert previous_period(DAY - timedelta(days=6), DAY) == (DAY - timedelta(days=13), DAY - timedelta(days=7))


def test_sync_backfills_gap_and_refreshes_recent():
    account = demo.ACCOUNTS["demo-ca"]
    assert import_dates({"synced_through": None}, account, 180, 30, DAY) == (DAY - timedelta(days=179), DAY)
    assert import_dates({"synced_through": DAY}, account, 180, 30, DAY) == (DAY - timedelta(days=29), DAY)
    assert import_dates({"synced_through": DAY - timedelta(days=90)}, account, 180, 30, DAY) == (
        DAY - timedelta(days=89), DAY)


class GoogleCredentials:
    def before_request(self, _request, _method, _url, headers):
        headers["authorization"] = "Bearer unit-test"


def test_google_read_only_manager_discovery_and_pagination():
    customer = "customers/1234567890"
    leaf = "customers/9876543210"
    metadata = {"descriptiveName": "Test", "currencyCode": "CAD", "timeZone": "America/Toronto"}
    seen = []

    def handle(request):
        import json
        seen.append(request.url.path)
        assert request.headers["authorization"] == "Bearer unit-test"
        if request.method == "GET":
            return httpx.Response(200, json={"resourceNames": [customer]})
        data = json.loads(request.content)
        if "from customer_client" in data["query"]:
            assert request.headers["login-customer-id"] == "1234567890"
            if data.get("pageToken") == "next":
                return httpx.Response(200, json={"results": [
                    {"customerClient": {**metadata, "clientCustomer": leaf, "status": "ENABLED"}}]})
            return httpx.Response(200, json={"nextPageToken": "next"})
        return httpx.Response(200, json={"results": [{"customer": {**metadata, "manager": True}}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        connector = GoogleAdsConnector(client, GoogleCredentials())
        result = connector.discover()
        assert [(item.resource, item.manager, item.currency) for item in result] == [(leaf, "1234567890", "CAD")]
        with pytest.raises(GoogleAdsError, match="Only Google Ads"):
            connector.request("POST", f"/{customer}/googleAds:mutate")
    assert len(seen) == 4


@pytest.mark.parametrize("status", [401, 403])
def test_google_revoked_permissions(status):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret"))) as client:
        with pytest.raises(GoogleAdsError, match="access was denied") as failure:
            GoogleAdsConnector(client, GoogleCredentials()).discover()
    assert "secret" not in str(failure.value)


def test_rate_limit_retried(monkeypatch):
    from backend.connectors import google_ads
    calls = []
    monkeypatch.setattr(google_ads.time, "sleep", lambda _seconds: None)

    def handle(_request):
        calls.append(1)
        return httpx.Response(429)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(GoogleAdsError, match="quota"):
            GoogleAdsConnector(client, GoogleCredentials()).discover()
    assert len(calls) == 4


def test_normalization_preserves_all_assets_and_order():
    headlines = [{"text": f"Headline {i}", "pinnedField": "HEADLINE_1" if i == 3 else "UNSPECIFIED"}
                 for i in range(15)]
    descriptions = [{"text": f"Description {i}"} for i in range(4)]
    row = {"adGroup": {"resourceName": "group"}, "adGroupAd": {
        "resourceName": "ad", "status": "ENABLED", "adStrength": "GOOD",
        "ad": {"id": "42", "type": "RESPONSIVE_SEARCH_AD", "finalUrls": ["https://example.com"],
               "responsiveSearchAd": {"headlines": headlines, "descriptions": descriptions, "path1": "shop"}},
    }}
    normalized = normalize_ad(row)
    creative = normalized.data["creative"]
    assert normalized.parent == "group"
    assert [a["text"] for a in creative["headlines"]] == [a["text"] for a in headlines]
    assert [a["text"] for a in creative["descriptions"]] == [a["text"] for a in descriptions]
    assert creative["headlines"][3]["pin"] == "HEADLINE_1"
    assert creative["headlines"][0]["pin"] == ""
    assert creative["path1"] == "shop"


@pytest.fixture
def demo_client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MODE", "demo")
    monkeypatch.setattr(auth, "MODE", "demo")
    monkeypatch.setenv("DEMO_DB", str(tmp_path / "drafts.sqlite3"))
    return TestClient(main.app)


def report(client, account="demo-ca"):
    end = demo.today(demo.ACCOUNTS[account]) - timedelta(days=1)
    return client.get(f"/orgs/demo/accounts/{account}/report",
                      params={"start": end - timedelta(days=29), "end": end})


def test_draft_persistence_and_isolation(demo_client):
    client = demo_client
    imported = report(client).json()["ads"][0]
    original = imported["data"]["creative"]
    creative = Creative.model_validate(original)
    assert not validate(creative)
    creative.headlines[1].text = "New Drone Store Headline"
    path = "/orgs/demo/accounts/demo-ca/drafts"
    params = {"resource": imported["resource"]}
    response = client.put(path, params=params, json={"creative": creative.model_dump()})
    assert response.status_code == 200
    version = response.json()["version"]
    assert client.put(path, params=params, json={"creative": creative.model_dump()}).status_code == 409
    assert TestClient(main.app).get(path).json()[0]["creative"] == creative.model_dump()
    assert report(client).json()["ads"][0]["data"]["creative"] == original
    assert client.get("/orgs/demo/accounts/demo-us/drafts").json() == []
    assert client.put(path.replace("demo-ca", "demo-us"), params=params,
                      json={"creative": creative.model_dump()}).status_code == 404
    updated = client.put(path, params=params, json={"creative": original, "version": version}).json()["version"]
    assert client.delete(path, params={**params, "version": version}).status_code == 409
    assert client.delete(path, params={**params, "version": updated}).status_code == 204


def test_demo_empty_and_organization_boundary(demo_client):
    assert demo_client.get("/orgs/other/accounts").status_code == 404
    response = demo_client.get("/orgs/demo/accounts/demo-ca/report",
                               params={"start": "2005-01-01", "end": "2005-01-07"})
    assert response.status_code == 200
    assert response.json()["performance"] == []
    assert demo_client.post("/orgs/demo/accounts/demo-ca/sync").status_code == 409


def test_rsa_validation():
    original = demo.snapshot("demo-ca", DAY, DAY)["ads"][0]["data"]["creative"]
    creative = Creative.model_validate(original)
    creative.headlines[1].text = creative.headlines[0].text.upper()
    creative.headlines[2].text = "飛" * 16
    for asset in creative.headlines:
        asset.pin = "HEADLINE_1"
    creative.descriptions[0].pin = "HEADLINE_2"
    creative.final_urls = ["javascript:alert(1)"]
    creative.path1, creative.path2 = "", "shop"
    issues = " ".join(validate(creative))
    for expected in ("duplicates", "character units", "every position", "unsupported pin", "HTTP", "path 1"):
        assert expected in issues


def test_live_api_requires_identity_and_membership(monkeypatch):
    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.setattr(auth, "MODE", "live")
    monkeypatch.setattr(main.repo, "memberships", lambda _user: [{"id": "tenant-a", "role": "member"}])
    monkeypatch.setattr(auth, "auth_call", lambda *_args, **_kwargs: {"id": "6fab9887-3954-4dce-83e2-3c445024cda1"})
    monkeypatch.setattr(main.repo, "accounts", lambda _org: [{"id": "account-a"}])
    monkeypatch.setattr(main, "configured_org", lambda: "tenant-a")
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer unit-test"}
    assert client.get("/orgs/tenant-a/accounts").status_code == 401
    assert client.get("/orgs/tenant-a/accounts", headers=headers).status_code == 200
    assert client.get("/orgs/tenant-b/accounts", headers=headers).status_code == 404
    assert client.get("/orgs/tenant-a/accounts/account-b/drafts", headers=headers).status_code == 404
    assert client.get("/orgs/tenant-a/discovery", headers=headers).status_code == 403


def test_supabase_token_error_is_sanitized(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://auth.example")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "test-key")
    monkeypatch.setattr(auth.httpx, "request", lambda *args, **kwargs:
                        httpx.Response(401, json={"message": "private upstream details"}))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as failure:
        auth.auth_call("GET", "user", token="revoked")
    assert failure.value.status_code == 401
    assert "private" not in failure.value.detail
