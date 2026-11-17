from frontend.organic_outcomes import TRACKED_URL, post_report


def test_black_friday_observation_and_missing_attribution():
    details = {"metadata": {"links": ["https://okoostore.com/pages/black-friday-watches"]},
               "snapshots": [{"observed_at": "2026-11-16", "api_version": "v25.0", "metrics": {
                   "post_media_view": 11, "post_total_media_view_unique": 4, "reactions": 2,
                   "post_clicks": 0, "availability": {"comments": "Permission unavailable"}}}], "daily": []}
    metrics, diagnostics, outcomes = post_report(details, "link")
    values = {row["metric"]: row["value"] for row in metrics}
    assert values["post_clicks"] == 0
    assert values["post_media_view"] == 11
    assert values["post_total_media_view_unique"] == 4
    assert all(values[key] is None for key in ("link_clicks", "outbound_clicks", "reach", "impressions", "shares"))
    assert diagnostics[0]["diagnostic"] == "Post-specific attribution unavailable: destination URL lacks unique UTM tagging."
    assert diagnostics[0]["suggested_future_url"] == TRACKED_URL
    assert outcomes == []


def test_tag_is_not_proof_of_unique_attribution():
    _, diagnostics, _ = post_report({"metadata": {"links": [TRACKED_URL]}}, "link")
    assert "must be verified" in diagnostics[0]["diagnostic"]


def test_destination_queries_have_separate_scopes(monkeypatch):
    import httpx
    from datetime import date
    from backend.connectors import content
    monkeypatch.setattr(content, "google_headers", lambda *args: {})
    requests = []
    def response(request):
        import json
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"rows": [], "rowCount": 0})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert list(content.analytics_reports(client, "123", "https://okoostore.com/pages/black-friday-watches",
                    date(2026, 11, 1), date(2026, 11, 16), [], destination_page=True)) == []
    assert len(requests) == 2
    landing, views = requests
    assert landing["dimensionFilter"]["andGroup"]["expressions"][0]["filter"]["fieldName"] == "landingPage"
    assert "ecommercePurchases" in [m["name"] for m in landing["metrics"]]
    assert "ecommercePurchases" not in [m["name"] for m in views["metrics"]]
    filters = views["dimensionFilter"]["andGroup"]["expressions"]
    assert filters[0]["filter"]["fieldName"] == "pagePath"
    assert filters[1]["filter"]["stringFilter"]["value"] == "page_view"
