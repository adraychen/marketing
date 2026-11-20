from datetime import date
from backend.connectors.google_ads import daily
from frontend.google_diagnostics import tables, share


def test_missing_zero_and_censored_share():
    row = {'segments': {'date': '2026-11-14'}, 'metrics': {'searchBudgetLostImpressionShare': 0, 'searchRankLostImpressionShare': .9001}}
    result = daily(row, 'campaigns', 'a', 'CAD')
    assert result.search_budget_lost_impression_share == 0
    assert share(result.search_rank_lost_impression_share) == '>90%'
    assert daily({'segments': row['segments']}, 'campaigns', 'a', 'CAD').search_rank_lost_impression_share is None
    assert share(None) == '—'


def test_scoped_diagnostics_dates_terms_and_daily_ranges():
    def row(resource, day, spend, kind='campaigns'):
        return dict(resource=resource, day=day, spend=spend, kind=kind, currency='CAD',
                    impressions=10, clicks=1, interactions=1, conversions=1, value=20,
                    search_budget_lost_impression_share=.2, search_rank_lost_impression_share=None)
    snapshot = dict(campaigns=[{'resource': 'a', 'data': {'name': 'A', 'daily_budget': 10, 'shared_budget': True}}],
        ad_groups=[{'resource': 'g', 'parent': 'a'}],
        search_terms=[{'resource': 't', 'parent': 'g', 'data': {'text': 'citizen watches'}}],
        performance=[row('a', '2026-11-14', 5), row('a', '2026-11-13', 999), row('t', '2026-11-14', 2, 'search_terms')])
    summary, days, terms = tables(snapshot, date(2026,11,14), date(2026,11,14), 'CAD')
    assert summary[0]['Spend'] == 'CAD 5.00'
    assert summary[0]['Current daily budget'] == 'CAD 10.00'
    assert summary[0]['Search lost IS (budget; daily range)'] == '20.00%'
    assert summary[0]['Search lost IS (rank; daily range)'] == '—'
    assert len(days) == 1
    assert terms[0]['Google Ads conversions'] == 1
    assert terms[0]['Checkout starts'] is None


def test_lost_share_requested_only_at_campaign_level():
    from backend.connectors.google_ads import GoogleAdsConnector
    from backend.models import Account
    connector = object.__new__(GoogleAdsConnector)
    queries = []
    connector.rows = lambda account, query: queries.append(query) or []
    connector.performance(Account(resource='customers/1', name='A', currency='CAD', timezone='UTC'), date(2026,11,1), date(2026,11,14))
    assert 'metrics.search_budget_lost_impression_share' in queries[0]
    assert 'metrics.search_rank_lost_impression_share' in queries[0]
    assert all('lost_impression_share' not in query for query in queries[1:])
