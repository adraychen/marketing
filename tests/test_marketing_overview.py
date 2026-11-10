from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from frontend import marketing_overview
from frontend.utm import build_utm_url


def test_utm_preserves_query_and_fragment_and_replaces_existing_tags():
    url = build_utm_url("https://okoostore.com/blog?variant=1&variant=2&utm_source=old#details",
                        "Facebook", "Citizen Eco-Drive", "Reel Light Powered")
    parts = urlsplit(url)
    assert parts.fragment == "details"
    assert parse_qs(parts.query) == {
        "variant": ["1", "2"], "utm_source": ["facebook"], "utm_medium": ["organic_social"],
        "utm_campaign": ["citizen_eco_drive"], "utm_content": ["reel_light_powered"],
    }


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://", "https://user:pass@example.org"])
def test_utm_rejects_invalid_destinations(url):
    with pytest.raises(ValueError):
        build_utm_url(url, "Facebook", "Citizen", "reel")


def test_uncovered_period_does_not_become_zero(monkeypatch):
    account = {"id": "1", "name": "Store", "currency": "CAD",
               "synced_from": "2026-11-08", "synced_through": "2026-11-10"}
    monkeypatch.setattr(marketing_overview, "call",
                        lambda path, **kwargs: [account] if path.endswith("/accounts") else {"performance": []})
    covered = list(marketing_overview.google_rows("/orgs/example", date(2026, 11, 9), date(2026, 11, 10)))[0]
    missing = list(marketing_overview.google_rows("/orgs/example", date(2026, 11, 1), date(2026, 11, 2)))[0]
    assert covered["Spend"] == 0 and covered["Previous spend"] is None
    assert missing["Spend"] is None and missing["Google Ads conversions"] is None
