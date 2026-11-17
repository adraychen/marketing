from datetime import date

from frontend.views import daily_chart_points


def test_calendar_axis_preserves_daily_values_gaps_and_original_comparison_dates():
    row = {'day': '2026-11-14', 'currency': 'CAD', 'spend': 12, 'impressions': 100,
           'clicks': 5, 'interactions': 5, 'conversions': 1, 'value': 30}
    prior = {**row, 'day': '2026-11-11', 'spend': 8}
    points = daily_chart_points([row], [prior], date(2026, 11, 13), date(2026, 11, 15), 'CAD')
    assert [p['Chart date'] for p in points[:3]] == [date(2026, 11, d) for d in (13, 14, 15)]
    assert [p['spend'] for p in points[:3]] == [None, 12, None]
    assert points[4]['Chart date'] == date(2026, 11, 14)
    assert points[4]['Date'] == date(2026, 11, 11)
    assert points[4]['spend'] == 8


def test_google_dashboard_renders_all_ten_charts():
    from streamlit.testing.v1 import AppTest
    def panel():
        from datetime import date
        from frontend.views import dashboard
        row = {'kind': 'campaigns', 'day': '2026-11-14', 'currency': 'CAD', 'spend': 12,
               'impressions': 100, 'clicks': 5, 'interactions': 5, 'conversions': 1, 'value': 30}
        account = {'currency': 'CAD', 'synced_from': '2026-11-01', 'synced_through': '2026-11-15'}
        dashboard({'performance': [row]}, account, '', date(2026, 11, 13), date(2026, 11, 15))
    app = AppTest.from_function(panel).run(timeout=20)
    assert not app.exception
    assert len(app.get('plotly_chart')) == 10
    assert not app.selectbox
