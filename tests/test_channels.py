from copy import deepcopy
from datetime import date
import json
import httpx
import pytest
from backend.channels import classify, owned_tags, coverage, comparisons
from backend import channel_data
from backend.meta_platforms import fetch_platforms, reconcile
from backend.connectors.meta_ads import MetaAdsError
from backend.connectors.content import ContentError


@pytest.mark.parametrize("source,medium,expected", [
    ("l.instagram.com", "referral", "Instagram Referral — Unattributed"),
    ("instagram", "organic_social", "Instagram Referral — Unattributed"),
    ("instagram", "paid_social", "Meta Paid — Instagram"),
    ("facebook", "paid_social", "Meta Paid — Facebook"),
    ("m.facebook.com", "referral", "Facebook Referral — Unattributed"),
    ("facebook", "organic_social", "Facebook Referral — Unattributed"),
    ("meta", "cpc", "Meta Paid — Platform Unknown"),
    ("google", "organic", "Google Organic Search"),
    ("google", "cpc", "Google Paid Search"),
    ("shopify_blog", "content", "Shopify Blog / Organic Search"),
    ("notinstagram.com", "referral", "Other Referral"),
    ("instagram.com.evil.example", "referral", "Other Referral"),
    ("newsletter", "email", "Direct / Unknown"),
    ("(direct)", "(none)", "Direct / Unknown"),
    ("", "", "Direct / Unknown"),
])
def test_channel_origin_is_not_owned_content(source, medium, expected):
    raw = {"source": source, "medium": medium, "campaign": "do-not-infer-a-post"}
    before = deepcopy(raw)
    result = classify(raw)
    assert result["channel"] == expected
    assert result["content_id"] is None
    assert result["raw_attribution"] == before == raw


def registry():
    return [{"source": "facebook", "external_id": "post1", "published_at": "2026-11-13T12:00:00-05:00",
             "data": {"links": ["https://okoostore.com/pages/black-friday-watches?utm_source=facebook&utm_medium=organic_social&utm_campaign=bf&utm_content=preview"]}}]


def test_unique_imported_tag_and_publication_evidence_required():
    raw = dict(source="facebook", medium="organic_social", campaign="bf", content="preview")
    known = owned_tags(registry())
    assert classify(raw, known, occurred_at="2026-11-14")["channel"] == "Facebook Organic Content"
    assert classify(raw, known, occurred_at="2026-11-12")["content_id"] is None
    assert classify(raw, known)["content_id"] is None
    assert classify({**raw, "content": "different"}, known, occurred_at="2026-11-14")["content_id"] is None
    items = registry()
    items.append({**items[0], "external_id": "post2"})
    assert owned_tags(items) == {}
    assert classify(dict(source="instagram", medium="organic_social", campaign="bf", content="preview"), known,
                    occurred_at="2026-11-14")["channel"] == "Instagram Referral — Unattributed"


def test_zero_unavailable_disconnected_and_untracked_are_distinct():
    rows = coverage({"coverage": [{"source": "ga4_store", "collected_at": "now"}],
                     "ga4": [{"channel": "Instagram Referral — Unattributed", "sessions": 0}]})
    ga = next(r for r in rows if r["channel"] == "Instagram Referral — Unattributed" and r["data_source"] == "GA4")
    assert ga["metric_states"]["sessions"] == "zero"
    assert ga["metric_states"]["ga4_purchases"] == "unavailable"
    assert any(r["state"] == "disconnected" for r in rows)
    assert any(r["state"] == "untracked" for r in rows)


def test_comparison_does_not_sum_providers_or_currencies():
    data = {"orders": [{"shop": "s", "channel": "Direct / Unknown", "currency": "CAD", "purchases": 1, "revenue": 314.5}],
            "ga4": [{"property_id": "p", "channel": "Direct / Unknown", "currency": "CAD", "ga4_purchases": 2, "ga4_purchase_revenue": 629, "sessions": 5}]}
    rows = comparisons(data)
    assert len(rows) == 2
    assert [r["purchases"] for r in rows] == [1, 2]
    assert rows[0]["sessions"] is None


def test_ga4_manual_utm_collection_and_missing_data(monkeypatch):
    monkeypatch.setattr(channel_data, "google_headers", lambda *args: {})
    dimensions = ["20261114", "l.instagram.com", "referral", "(referral)", "(not set)", "(not set)", "(not set)", "(not set)", "(not set)"]
    captured = []
    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"metadata": {"timeZone": "America/Toronto", "currencyCode": "CAD"},
            "rows": [{"dimensionValues": [{"value": x} for x in dimensions],
                      "metricValues": [{"value": x} for x in ["3", "2", "0", "0", "0", "0"]]}], "rowCount": 1})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        rows, currency = channel_data.collect(client, "123", "America/Toronto", date(2026, 11, 1), date(2026, 11, 16))
    report = channel_data.report_rows(rows, owned_tags(registry()), "America/Toronto")
    assert len(captured[0]["dimensions"]) == 9
    assert report[0]["channel"] == "Instagram Referral — Unattributed"
    assert report[0]["ga4_purchases"] == 0
    assert report[0]["original_utm"]["content"] == "(not set)"
    assert currency == "CAD"


def test_platform_breakdown_exact_purchase_no_reach_rollup():
    requests = []
    raw = {"ad_id": "a", "adset_id": "s", "campaign_id": "c", "date_start": "2026-11-14", "date_stop": "2026-11-14",
           "publisher_platform": "instagram", "spend": "102.28", "reach": "20",
           "actions": [{"action_type": "purchase", "value": "1"}, {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "1"}],
           "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "314.50"}]}
    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [raw]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        rows = fetch_platforms(client, {"version": "v26.0", "token": "test", "account_id": "123"}, date(2026, 11, 1), date(2026, 11, 16))
    assert requests[0].url.params["breakdowns"] == "publisher_platform"
    assert requests[0].url.params["use_unified_attribution_setting"] == "true"
    assert requests[0].url.params["action_report_time"] == "impression"
    assert rows[0]["website_purchases"] == 1
    assert rows[0]["landing_page_views"] is None
    assert reconcile(rows, rows) == []
    issues = reconcile([{**rows[0], "website_purchases": 2, "reach": 500}], rows)
    assert [i["metric"] for i in issues] == ["website_purchases"]
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": [raw, raw]}))) as client:
        with pytest.raises(MetaAdsError, match="duplicate"):
            fetch_platforms(client, {"version": "v26.0", "token": "test", "account_id": "123"}, date(2026, 11, 1), date(2026, 11, 16))


def test_repeated_channel_sync_replaces_window_without_appending(monkeypatch):
    from contextlib import contextmanager
    statements, writes = [], []
    class Cursor:
        def execute(self, sql, params=None):
            statements.append((sql, params))
        def fetchone(self):
            return {"present": "marketing_channel_daily"}
    @contextmanager
    def database():
        yield Cursor()
    monkeypatch.setattr(channel_data, "database", database)
    sample = [{"day": date(2026, 11, 14), "segment": "{}", "dimensions": {}, "metrics": {"sessions": 1}}]
    monkeypatch.setattr(channel_data, "collect", lambda *args: (sample, "CAD"))
    monkeypatch.setattr(channel_data, "execute_values", lambda cur, sql, rows: writes.append(rows))
    for _ in range(2):
        assert channel_data.sync(None, "tenant-a", "123", "America/Toronto", 30) == 1
    deletes = [p for sql, p in statements if sql.startswith("delete")]
    assert len(deletes) == 2 and deletes[0] == deletes[1]
    assert deletes[0][:2] == ("tenant-a", "123")
    assert writes[0] == writes[1] or len(writes[0]) == len(writes[1]) == 1
    statements.clear()
    monkeypatch.setattr(channel_data, "collect", lambda *args: (_ for _ in ()).throw(ContentError("restricted")))
    with pytest.raises(ContentError):
        channel_data.sync(None, "tenant-a", "123", "America/Toronto", 30)
    assert not any(sql.startswith("delete") for sql, _ in statements)


def test_channel_export_preserves_source_and_zero_vs_missing(monkeypatch):
    import io
    from zipfile import ZipFile
    from frontend import reports
    report = reports.Report()
    report.table("Channel evidence", [{**classify({"source": "l.instagram.com", "medium": "referral"}),
                                       "sessions": 2, "purchases": 0, "outbound_clicks": None}])
    document, extension, _ = report.export()
    assert extension == "md"
    assert b"l.instagram.com" in document and b"Instagram Referral" in document
    assert b"| 2 | 0 |  |" in document
    monkeypatch.setattr(reports, "INLINE_LIMIT", 1)
    document, extension, _ = report.export()
    assert extension == "zip"
    with ZipFile(io.BytesIO(document)) as bundle:
        content = bundle.read("01-channel-evidence.csv").decode("utf-8-sig")
        assert "l.instagram.com" in content and "referral" in content
