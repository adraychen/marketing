import json
from datetime import date

import httpx

from backend import review_ai
from backend.review import weekly_review

START, END = date(2026, 10, 30), date(2026, 11, 5)
ACCOUNT = {"currency": "CAD", "synced_from": "2026-10-23", "synced_through": "2026-11-05"}


def snapshot():
    campaign = {"resource": "c", "data": {"name": "Private campaign", "daily_budget": 35}}
    group = {"resource": "g", "parent": "c", "data": {"name": "Search ad group"}}
    term = {"resource": "s", "parent": "g", "data": {"text": "Private search query"}}
    base = {"currency": "CAD", "clicks": 30, "impressions": 100, "interactions": 30}
    return {
        "campaigns": [campaign], "ad_groups": [group], "keywords": [], "search_terms": [term],
        "performance": [
            dict(base, kind="campaigns", resource="c", day="2026-10-23", spend=80, conversions=4, value=200),
            dict(base, kind="campaigns", resource="c", day="2026-11-05", spend=130, conversions=2, value=None),
            dict(base, kind="search_terms", resource="s", day="2026-11-05", spend=55, conversions=0, value=0),
            dict(base, kind="campaigns", resource="c", day="2026-11-06", spend=999, conversions=0, value=0),
        ],
    }


def test_review_compares_same_length_windows_and_ranks_signals():
    review = weekly_review(snapshot(), ACCOUNT, START, END)
    assert review["prior_start"] == "2026-10-23"
    assert review["prior_end"] == "2026-10-29"
    assert review["current"]["spend"] == 130
    assert review["previous"]["roas"] == 2.5
    assert review["current"]["roas"] is None
    assert [action["kind"] for action in review["actions"]] == ["search_term", "campaign"]
    assert review["actions"][0]["ad_group"] == "Search ad group"
    assert review["drivers"]["campaigns"][0]["value_delta"] is None


def test_review_withholds_recommendations_without_coverage_or_enough_clicks():
    report = snapshot()
    incomplete = weekly_review(report, {**ACCOUNT, "synced_from": "2026-10-24"}, START, END)
    assert not incomplete["coverage"] and incomplete["actions"] == []
    report["performance"][2]["clicks"] = 19
    review = weekly_review(report, ACCOUNT, START, END)
    assert [action["kind"] for action in review["actions"]] == ["campaign"]
    report["performance"] = []
    empty = weekly_review(report, ACCOUNT, START, END)
    assert empty["coverage"] and not empty["has_activity"]


def test_ai_summary_sends_only_anonymized_evidence(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_REVIEW_MODEL", "test-model")

    def post(url, **kwargs):
        assert url == "https://api.openai.com/v1/responses"
        payload = kwargs["json"]
        assert payload["store"] is False
        assert "Private" not in payload["input"]
        evidence = json.loads(payload["input"])
        assert evidence["signals"][0]["current"]["spend"] == 55
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Review the change."}]}],
        })

    monkeypatch.setattr(review_ai.httpx, "post", post)
    assert review_ai.explain(weekly_review(snapshot(), ACCOUNT, START, END), "CAD") == "Review the change."
