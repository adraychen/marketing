from datetime import date
from unittest.mock import MagicMock, patch

from frontend.views import dashboard


def test_campaign_selection_scopes_cards_comparison_table_and_charts():
    campaigns = [{'resource': key, 'data': {'id': key, 'name': key}} for key in ('a', 'b')]
    def row(key, day, spend):
        return dict(resource=key, kind='campaigns', day=day, currency='CAD', spend=spend,
                    impressions=100, clicks=5, interactions=5, conversions=1, value=30)
    report = dict(campaigns=campaigns, performance=[row('a', '2026-11-14', 12),
                  row('b', '2026-11-14', 99), row('a', '2026-11-13', 8)])
    account = dict(currency='CAD', synced_from='2026-11-01', synced_through='2026-11-15')
    ui = MagicMock()
    columns = []
    def make_columns(count):
        result = [MagicMock() for _ in range(count)]
        columns.extend(result)
        return result
    ui.columns.side_effect = make_columns
    ui.selectbox.return_value = 'a'
    with patch('frontend.views.st', ui), patch('frontend.views.table') as table:
        dashboard(report, account, '/account/one', date(2026, 11, 14), date(2026, 11, 14))
    assert columns[0].metric.call_args.args[:3] == ('Spend', 'CAD 12.00', '+CAD 4.00')
    assert table.call_args_list[0].args[0][0]['Spend'] == 'CAD 12.00'
    assert len(table.call_args_list[0].args[0]) == 1
    assert ui.plotly_chart.call_count == 10
    assert list(ui.plotly_chart.call_args_list[0].args[0].data[0].y) == [12]
    assert all(call.args[0] <= 3 for call in ui.columns.call_args_list)
    assert len(report['performance']) == 3
