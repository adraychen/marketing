import json
from datetime import date
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException

from backend import main, review_ai
from backend.review import weekly_review
from frontend import weekly_review as review_view

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
    assert review["ai_ready"]
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
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.delenv("GROQ_REVIEW_MODEL", raising=False)

    def post(url, **kwargs):
        assert url == "https://api.groq.com/openai/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer test"
        payload = kwargs["json"]
        assert payload["model"] == "openai/gpt-oss-120b"
        assert review_ai.FOCUS_PROMPTS["strategies"] in payload["messages"][0]["content"]
        content = payload["messages"][1]["content"]
        assert "Private" not in content
        evidence = json.loads(content)
        assert evidence["campaigns"][0]["alias"] == "Campaign 1"
        assert evidence["signals"][0]["current"]["spend"] == 55
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"finish_reason": "stop", "message": {"content": "Review the change."}}],
        })

    monkeypatch.setattr(review_ai.httpx, "post", post)
    result = review_ai.explain(weekly_review(snapshot(), ACCOUNT, START, END), "CAD", "strategies")
    assert result == {
        "summary": "Review the change.",
        "model": "openai/gpt-oss-120b",
        "campaign_labels": {"Campaign 1": "Private campaign"},
    }


@pytest.mark.parametrize("period_index", [0, 1])
def test_ai_endpoint_does_not_send_low_traffic_weeks_to_groq(monkeypatch, period_index):
    report = snapshot()
    report["performance"][period_index]["clicks"] = 3
    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.setattr(main, "review_ai_enabled", lambda: True)
    monkeypatch.setattr(main.repo, "snapshot", lambda *_args: report)
    monkeypatch.setattr(main, "explain", lambda *_args: pytest.fail("Groq should not be called"))

    with pytest.raises(HTTPException, match="20 clicks in each week") as exc:
        main.weekly_review_ai("org", "account", END, {**ACCOUNT, "timezone": "America/Toronto"}, {"role": "admin"})
    assert exc.value.status_code == 409


def test_weekly_view_handles_review_without_ai_ready(monkeypatch):
    report = snapshot()
    report["performance"][1]["clicks"] = 3
    older_review = weekly_review(report, ACCOUNT, START, END)
    del older_review["ai_ready"]
    ui = MagicMock()
    ui.date_input.return_value = END
    ui.radio.return_value = "Campaigns"
    ui.selectbox.return_value = "Spend"
    monkeypatch.setattr(review_view, "st", ui)
    monkeypatch.setattr(review_view, "call", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(review_view, "weekly_review", lambda *_args: older_review)
    monkeypatch.setattr(review_view, "table", lambda _rows: None)

    review_view.show_weekly_review(ACCOUNT, "/test", END, ai_available=True)

    assert "3 clicks this week" in ui.info.call_args.args[0]
    ui.button.assert_not_called()
