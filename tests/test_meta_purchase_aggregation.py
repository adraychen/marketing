"""Nov 1–14 regression reconstructed from user-supplied figures, not a live export."""
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest

from backend import meta_ads
from backend.connectors.meta_ads import normalize_metric
from frontend import meta_ads as ui
from frontend.reports import Report


def daily(day, spend, count=None, value=None, ad='a1', campaign='c1', adset='g1'):
    raw = {'ad_id': ad, 'campaign_id': campaign, 'adset_id': adset, 'date_start': day,
           'spend': str(spend), 'attribution_setting': '7d_click,1d_view',
           'actions': [{'action_type': 'purchase', 'value': '99'}, {'action_type': 'omni_purchase', 'value': '99'}],
           'action_values': [{'action_type': 'purchase', 'value': '9999'}]}
    if count is not None:
        raw['actions'].append({'action_type': 'offsite_conversion.fb_pixel_purchase', 'value': str(count)})
    if value is not None:
        raw['action_values'].append({'action_type': 'offsite_conversion.fb_pixel_purchase', 'value': str(value)})
    return normalize_metric(raw)


def report(rows):
    return {**meta_ads.empty_report(), 'coverage': True,
            'campaigns': [{'id': c, 'name': c} for c in sorted({r['campaign_id'] for r in rows})], 'daily': rows}


def regression():
    return report([daily('2026-11-01', 90), daily('2026-11-14', 12.28, 1, 314.50)])


def test_nov_1_14_overview_campaign_and_export(monkeypatch):
    data = regression()
    for summary in (ui.totals(data), ui.campaigns(data)[0]):
        assert summary['spend'] == pytest.approx(102.28)
        assert summary['website_purchases'] == 1
        assert summary['website_purchase_value'] == 314.50
        assert summary['website_cpa'] == pytest.approx(102.28)
        assert summary['website_roas'] == pytest.approx(3.07489245)
        assert summary['website_purchases_missing_rows'] == 1
        assert summary['website_purchases_status'] == 'Reported subtotal'
    # Isolate the existing Meta export sections; do not invoke AI or extra evaluation.
    from frontend import campaign_evaluation
    monkeypatch.setattr(campaign_evaluation, 'load', lambda *a: (_ for _ in ()).throw(ui.APIError('not requested in this test')))
    exported = Report()
    ui.add_meta_report(exported, '/orgs/test', date(2026, 11, 1), date(2026, 11, 14), ['c1'], data=data)
    tables = dict(exported.sections)
    assert tables['Meta paid overview'][0]['website_purchases'] == 1
    assert tables['Meta paid campaign performance'][0]['website_purchases'] == 1
    assert tables['Meta paid overview'][0]['website_purchase_value'] == 314.50
    assert '314.5' in exported.export()[0].decode()


@pytest.mark.parametrize('rows,expected', [
    ([daily('2026-11-01', 10)], None),
    ([], None),
    ([daily('2026-11-01', 10, 0, 0)], 0),
    ([daily('2026-11-01', 10, 0, 0), daily('2026-11-02', 5)], None),
    ([daily('2026-11-01', 10, 2, 200), daily('2026-11-02', 5, 3, 300)], 5),
])
def test_missing_zero_and_multiple_days(rows, expected):
    result = ui.totals(report(rows))
    assert result['website_purchases'] == expected
    if expected is None:
        assert result['website_purchase_value'] is None
    if not expected:
        assert result['website_cpa'] is None


def test_multiple_ads_adsets_and_campaigns_keep_identifiers_and_attribution():
    data = report([daily('2026-11-14', 10, 1, 100),
                   daily('2026-11-14', 20, 2, 200, 'a2', 'c1', 'g2'),
                   daily('2026-11-14', 30, 3, 300, 'a3', 'c2', 'g3')])
    assert ui.totals(data)['website_purchases'] == 6
    campaigns = ui.campaigns(data)
    assert [r['website_purchases'] for r in campaigns] == [3, 3]
    assert ui.totals(ui.scoped(data, ['c1']))['website_purchase_value'] == 300
    assert campaigns[0]['purchase_attribution_settings'] == ['7d_click,1d_view']
    data['daily'][1]['attribution_setting'] = '1d_click'
    assert ui.campaigns(data)[0]['purchase_attribution_settings'] == ['1d_click', '7d_click,1d_view']


def test_purchase_value_missing_is_not_inferred_from_count():
    result = ui.totals(report([daily('2026-11-14', 10, 1)]))
    assert result['website_purchases'] == 1
    assert result['website_purchase_value'] is None
    assert result['website_cpa'] == 10
    assert result['website_roas'] is None


def test_partial_period_keeps_observed_purchase_but_no_spend_ratios():
    data = regression()
    data['coverage'] = False
    result = ui.totals(data)
    assert result['website_purchases'] == 1
    assert result['website_cpa'] is None and result['website_roas'] is None


def test_repeated_sync_replaces_rows_and_report_filters_selected_dates(monkeypatch):
    stored = {}
    account = {'account_id': '123', 'data': {'currency': 'CAD'}, 'synced_from': date(2026, 11, 1),
               'synced_through': date(2026, 11, 15), 'synced_at': datetime.now(timezone.utc)}
    class Cursor:
        def execute(self, sql, params):
            self.sql, self.params = sql, params
            if sql.startswith('delete from marketing_meta_ads_daily'):
                org, acct, start, end = params
                for key in list(stored):
                    if key[:2] == (org, acct) and str(start) <= key[3] <= str(end):
                        del stored[key]
        def fetchall(self):
            if 'from marketing_meta_ads_accounts' in self.sql:
                return [account]
            if 'from marketing_meta_ads_resources' in self.sql:
                return []
            assert 'day between %s and %s' in self.sql
            org, acct, start, end = self.params
            return [{'data': v} for k, v in stored.items() if k[:2] == (org, acct) and str(start) <= k[3] <= str(end)]
    def insert(cur, sql, rows):
        if 'marketing_meta_ads_daily' in sql:
            for row in rows:
                assert row[:4] not in stored
                stored[row[:4]] = row[-1].adapted
    @contextmanager
    def db(): yield Cursor()
    monkeypatch.setattr(meta_ads, 'execute_values', insert)
    monkeypatch.setattr(meta_ads, 'database', db)
    monkeypatch.setattr(meta_ads, 'configuration', lambda org: {'account_id': '123'})
    rows = regression()['daily'] + [daily('2026-11-15', 10, 9, 900)]
    config = {'account_id': '123', 'version': 'v26.0', 'token_expires_at': None}
    for _ in range(2):
        meta_ads.write_import(Cursor(), 'org', config, {'currency': 'CAD'}, {}, rows, date(2026, 11, 1), date(2026, 11, 15))
    assert len(stored) == 3
    selected = meta_ads.report('org', date(2026, 11, 1), date(2026, 11, 14))
    assert len(selected['daily']) == 2
    assert ui.totals(selected)['website_purchases'] == 1
    assert ui.campaigns(selected)[0]['website_purchase_value'] == 314.50
