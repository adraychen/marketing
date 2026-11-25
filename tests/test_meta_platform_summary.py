from unittest.mock import MagicMock, patch

from backend.meta_ads import empty_report
from frontend.meta_ads import platform_scope, platform_summary, scoped, totals
from frontend.marketing_overview import show_paid_summary


def sample():
    rows = [dict(ad_id='a', campaign_id='c', day='2026-11-20', publisher_platform=p,
                 spend=s, impressions=i, clicks=c, inline_link_clicks=c,
                 website_purchases=None, website_purchase_value=None)
            for p, s, i, c in [('facebook', 12, 100, 10), ('instagram', 8, 400, 5), ('audience_network', 2, 50, 1)]]
    return {**empty_report(), 'account': {'data': {'name': 'Shop', 'currency': 'CAD'}},
            'platform_daily': rows, 'platform_coverage': True,
            'platform_status': {'state': 'available', 'last_successful_sync': '2026-11-21'},
            'campaigns': [{'id': 'c'}], 'coverage': True}


def test_platform_metrics_ratios_and_other_placements():
    data = sample()
    facebook, instagram, other = platform_summary(data)
    assert facebook['Spend'] == 12
    assert instagram['Spend'] == 8
    assert facebook['CTR'] == .1
    assert instagram['CTR'] == .0125
    assert other['Spend'] == 2
    assert facebook['Meta website purchases'] is None
    assert totals(platform_scope(data, 'instagram'))['clicks'] == 5
    assert platform_scope(data, None) is data
    assert platform_scope(scoped(data, []), 'facebook')['daily'] == []


def test_missing_breakdown_is_unknown_and_failed_refresh_is_flagged():
    assert all(r['Spend'] is None for r in platform_summary(empty_report()))
    data = sample()
    data['platform_status']['state'] = 'unavailable'
    assert platform_summary(data)[0]['Status'] == 'Stale import'
    assert platform_summary(data)[0]['Spend'] == 12
    data['platform_daily'] = []
    data['platform_coverage'] = False
    assert platform_summary(data)[0]['Spend'] is None


def test_overview_cards_use_combined_totals_once_even_if_breakdown_differs():
    ui = MagicMock()
    columns = [MagicMock() for _ in range(3)]
    ui.columns.return_value = columns
    with patch('frontend.marketing_overview.st', ui), patch('frontend.marketing_overview.table') as table:
        show_paid_summary([{'Platform': 'Meta Ads', 'Currency': 'CAD', 'Spend': 25,
                            'Impressions': 600, 'Clicks': 20, 'Status': 'Imported'}], platform_summary(sample()))
    assert columns[0].metric.call_args.args[1] == 'CAD 25.00'
    summary = table.call_args_list[0].args[0]
    assert [r['Platform'] for r in summary] == ['Facebook Ads', 'Instagram Ads', 'Meta · Audience Network']
    assert summary[0]['Spend'] == 'CAD 12.00'
