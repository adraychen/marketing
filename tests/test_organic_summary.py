from frontend.organic_summary import post_total


def test_reported_zero_is_preserved():
    assert post_total([{"metrics": {"post_clicks": 0}}], ("post_clicks",)) == "0"


def test_missing_item_metric_does_not_produce_partial_total():
    rows = [{"metrics": {"post_media_view": 11}}, {"metrics": None}]
    assert post_total(rows, ("post_media_view",)) == "—"
    assert post_total([], ("post_media_view",)) == "—"


def test_interactions_require_every_component():
    rows = [{"metrics": {"reactions": 2, "shares": 1}}]
    assert post_total(rows, ("reactions", "comments", "shares")) == "—"
    rows[0]["metrics"]["comments"] = 0
    assert post_total(rows, ("reactions", "comments", "shares")) == "3"


def test_additive_metrics_across_posts():
    rows = [{"metrics": {"post_media_view": 11}}, {"metrics": {"post_media_view": 4}}]
    assert post_total(rows, ("post_media_view",)) == "15"
