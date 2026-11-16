from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import campaign_evaluation as ev, commerce, evaluation_data, main, review_ai
from frontend.campaign_evaluation import add_report
from frontend.reports import Report


def fixture():
    row = {'campaign_id': 'c1', 'ad_id': 'a1', 'day': '2026-11-11', 'spend': 4.16, 'impressions': 171,
           'inline_link_clicks': 6, 'attribution_setting': '7d_click,1d_view',
           'actions': [{'action_type': 'landing_page_view', 'value': '4'},
                       {'action_type': 'offsite_conversion.fb_pixel_view_content', 'value': '5'},
                       {'action_type': 'view_content', 'value': '5'}]}
    url = 'https://okoostore.com/products/promaster?utm_source=facebook&utm_medium=paid_social&utm_campaign=dive'
    data = {'campaigns': [{'id': 'c1', 'name': 'Promaster Dive'}], 'adsets': [],
            'ads': [{'id': 'a1', 'campaign_id': 'c1', 'creative': {'link_url': url}}],
            'daily': [row], 'coverage': True, 'stale': False,
            'account': {'account_id': 'acct', 'synced_at': '2026-11-12', 'data': {'currency': 'CAD', 'timezone_name': 'America/Toronto'}}}
    variant = {'id': 'v1', 'product': {'id': 'p1', 'handle': 'promaster', 'title': 'Promaster'},
               'inventoryItem': {'unitCost': {'amount': '40', 'currencyCode': 'CAD'}}}
    extra = {'orders': [], 'cost_models': [], 'warnings': [], 'snapshots': [
        {'source': 'shopify_products', 'account': 'store', 'complete': True, 'stale': False, 'data': {'variants': [variant]}},
        {'source': 'ga4_landing', 'account': 'ga', 'complete': True, 'stale': False, 'data': {
            'currency': 'CAD', 'timezone': 'America/Toronto', 'rows': [
                {'day': '20261111', 'source': 'facebook', 'medium': 'paid_social', 'campaign': 'dive',
                 'host': 'okoostore.com', 'landing_page': '/products/promaster?variant=1',
                 'metrics': dict(zip(ev.GA_METRICS, [4, 5, 2, 1, 1, 100]))}]}}]}
    outcomes = {'coverage': [{'source': 'shopify_orders', 'account': 'store', 'currency': 'CAD', 'timezone': 'America/Toronto', 'complete': True},
                             {'source': 'ga4_store', 'account': 'ga', 'currency': 'CAD', 'timezone': 'America/Toronto', 'complete': True}],
                'ga4': [{'source': 'facebook', 'medium': 'paid_social', 'campaign': 'dive', 'ga4_purchases': 1}], 'latest_runs': []}
    return data, deepcopy(data), outcomes, extra


def evaluate(args):
    return ev.evaluate(*args, {}, date(2026, 11, 1), date(2026, 11, 11), 'https://okoostore.com')


def test_promaster_funnel_uses_original_categories_and_no_false_journey():
    result = evaluate(fixture())['campaigns'][0]
    rows = result['funnel']
    assert [r['count'] for r in rows] == [171, 6, 4, 5, None, None, None]
    assert rows[1]['preceding_stage_rate'] == pytest.approx(6/171)
    assert rows[2]['cost_per_action'] == 1.04
    assert rows[3]['cost_per_action'] == pytest.approx(.832)
    assert rows[3]['preceding_stage_rate'] is None
    assert rows[6]['cost_per_action'] is None
    assert result['confidence'] == 'Low'
    assert result['diagnostics'][2]['status'] == 'Data unavailable'
    assert result['funnel'][2]['change'] == 0


def test_partial_fields_do_not_turn_into_zero_or_complete_ratios():
    args = fixture()
    args[0]['daily'].append({'campaign_id': 'c1', 'day': '2026-11-10', 'spend': 1, 'impressions': 10})
    result = evaluate(args)['campaigns'][0]['funnel']
    assert result[2]['count'] == 4
    assert result[2]['availability'] == 'Partial reported count'
    assert result[2]['cost_per_action'] is None
    assert result[2]['change'] is None


def test_incomplete_date_coverage_withholds_derived_metrics():
    args = fixture()
    args[0]['coverage'] = False
    rows = evaluate(args)['campaigns'][0]['funnel']
    assert rows[1]['count'] == 6
    assert rows[1]['preceding_stage_rate'] is None
    assert rows[1]['cost_per_action'] is None


def test_missing_denominator_and_mixed_attribution_withhold_rates_and_changes():
    args = fixture()
    args[0]['daily'].append({'campaign_id': 'c1', 'day': '2026-11-10', 'inline_link_clicks': 2,
                             'spend': 1, 'attribution_setting': '1d_click'})
    rows = evaluate(args)['campaigns'][0]['funnel']
    assert rows[1]['preceding_stage_rate'] is None
    assert rows[1]['change'] is None


def test_shared_campaign_tags_never_double_credit_orders_or_ga4():
    args = fixture()
    args[0]['campaigns'].append({'id': 'c2', 'name': 'Copy'})
    args[0]['ads'].append({**args[0]['ads'][0], 'id': 'a2', 'campaign_id': 'c2'})
    result = evaluate(args)
    assert all(c['profitability']['shopify_attributed_orders'] is None for c in result['campaigns'])
    assert all(c['product_outcomes'] == [] for c in result['campaigns'])
    assert result['unallocated_ga4_traffic'][0]['sessions'] == 4


def test_product_landing_outcomes_require_verified_host_and_product():
    args = fixture()
    result = evaluate(args)['campaigns'][0]
    assert result['product_outcomes'][0]['itemViewEvents'] == 5
    assert result['product_outcomes'][0]['source'] == 'GA4 product-landing sessions'
    args[3]['snapshots'][1]['data']['rows'][0]['host'] = 'other.example'
    assert evaluate(args)['campaigns'][0]['product_outcomes'] == []


def test_unknown_sources_retained_and_incomplete_ga4_not_allocated():
    args = fixture()
    args[3]['snapshots'][1]['data']['rows'][0]['source'] = 'unknown-network'
    assert evaluate(args)['unallocated_ga4_traffic'][0]['source'] == 'unknown-network'
    args[3]['snapshots'][1]['complete'] = False
    assert evaluate(args)['campaigns'][0]['product_outcomes'] == []


def test_dynamic_ids_and_conflicting_tags_are_handled_conservatively():
    data, _, _, extra = fixture()
    ad = data['ads'][0]
    ad['creative'] = {'link_url': 'https://okoostore.com/products/promaster',
                      'url_tags': 'utm_source=facebook&utm_medium=paid_social&utm_campaign={{campaign.id}}&utm_content={{ad.id}}'}
    links = ev.destinations(data, extra['snapshots'][0]['data']['variants'], 'https://okoostore.com')
    assert links[0]['utm']['campaign'] == 'c1'
    assert links[0]['utm']['content'] == 'a1'
    ad['creative']['link_url'] += '?utm_campaign=other'
    assert ev.destinations(data, [], 'https://okoostore.com')[0]['matchable'] is False


def test_malformed_destination_does_not_break_campaign_report():
    args = fixture()
    args[0]['ads'][0]['creative']['link_url'] = 'https://[broken'
    result = evaluate(args)['campaigns'][0]
    assert result['destinations'][0]['product_id'] is None
    assert any('unverified' in q for q in result['quality'])


def order():
    return {'shop': 'store', 'order_id': 'o1', 'currency': 'CAD', 'amount': Decimal('113'),
            'utm': {'source': 'facebook', 'medium': 'paid_social', 'campaign': 'dive'},
            'evaluation': {'tax': {'amount': '13', 'currencyCode': 'CAD'},
                           'line_items': {'nodes': [{'currentQuantity': 1, 'variant': {'id': 'v1'}}], 'pageInfo': {'hasNextPage': False}}}}


def model():
    return {'currency': 'CAD', 'shipping_per_order': 10, 'payment_rate': .03,
            'payment_per_order': .30, 'other_per_order': 2, 'note': 'Verified invoice and fee schedule'}


def test_profit_math_uses_order_tax_current_cost_and_explicit_fees():
    args = fixture()
    args[3]['orders'] = [order(), order()]  # Deduplicate the same canonical Shopify order.
    args[3]['cost_models'] = [{'account_id': 'acct', 'campaign_id': 'c1', 'data': model()}]
    p = evaluate(args)['campaigns'][0]['profitability']
    assert p['shopify_attributed_orders'] == 1
    assert p['shopify_attributed_revenue'] == 113
    assert p['product_cost'] == 40
    assert p['modeled_variable_costs'] == 15.69
    assert p['estimated_contribution'] == 40.15  # 113 - 13 - 40 - 15.69 - 4.16
    assert p['break_even_acquisition_cost'] == 44.31
    assert p['shopify_cost_per_order'] == 4.16
    assert p['meta_attributed_purchases'] is None


@pytest.mark.parametrize('missing', ['cost', 'tax', 'model', 'currency', 'truncated'])
def test_missing_profit_inputs_are_never_inferred(missing):
    args = fixture()
    args[3]['orders'] = [order()]
    args[3]['cost_models'] = [{'account_id': 'acct', 'campaign_id': 'c1', 'data': model()}]
    if missing == 'cost':
        args[3]['snapshots'][0]['data']['variants'][0]['inventoryItem']['unitCost'] = None
    elif missing == 'tax':
        args[3]['orders'][0]['evaluation']['tax'] = None
    elif missing == 'model':
        args[3]['cost_models'] = []
    elif missing == 'currency':
        args[3]['orders'][0]['currency'] = 'USD'
    else:
        args[3]['orders'][0]['evaluation']['line_items']['pageInfo']['hasNextPage'] = True
    assert evaluate(args)['campaigns'][0]['profitability']['estimated_contribution'] is None


def test_timezone_mismatch_withholds_cross_source_ratios():
    args = fixture()
    args[3]['orders'] = [order()]
    args[2]['coverage'][0]['timezone'] = 'Asia/Hong_Kong'
    p = evaluate(args)['campaigns'][0]['profitability']
    assert p['shopify_attributed_orders'] == 1
    assert p['shopify_cost_per_order'] is None
    assert p['shopify_roas'] is None


def test_cost_validation_rejects_unknown_fields_negative_and_nonfinite_values():
    for values in ({**model(), 'payment_rate': 1.1}, {**model(), 'shipping_per_order': -1},
                   {**model(), 'other_per_order': float('nan')}, {**model(), 'currency': 'cad'},
                   {**model(), 'guessed_margin': .5}):
        with pytest.raises(ValidationError):
            evaluation_data.CostModel(**values)


def test_export_contains_campaign_evidence_and_scopes_selection():
    evidence = evaluate(fixture())
    report = Report()
    add_report(report, evidence, ['c1'])
    text, ext, _ = report.export()
    assert ext == 'md'
    for value in ('Promaster Dive', '171.0', 'landing_page_view', 'Profitability', 'Data unavailable'):
        assert value in text.decode()
    excluded = Report()
    add_report(excluded, evidence, [])
    assert 'Promaster Dive' not in excluded.export()[0].decode()


def test_ai_payload_excludes_identifiers_urls_and_cost_notes(monkeypatch):
    campaign = evaluate(fixture())['campaigns'][0]
    campaign['profitability']['cost_model'] = model()
    evidence = json.dumps(review_ai.campaign_evidence(campaign))
    for secret in ('Promaster', 'okoostore.com', 'invoice', 'c1', 'a1'):
        assert secret not in evidence
    captured = {}
    def post(url, **kwargs):
        captured.update(kwargs['json'])
        return httpx.Response(200, request=httpx.Request('POST', url), json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'Monitor the 6 link clicks; confidence is low.'}}]})
    monkeypatch.setattr(review_ai.httpx, 'post', post)
    assert '6 link clicks' in review_ai.explain_campaign(campaign)['summary']
    assert 'six short sections' in captured['messages'][0]['content']


def test_new_routes_enforce_membership_and_admin(monkeypatch):
    monkeypatch.setattr(main, 'MODE', 'live')
    main.app.dependency_overrides[main.member] = lambda: {'role': 'member'}
    try:
        client = TestClient(main.app)
        assert client.put('/orgs/other/meta-ads/campaigns/c1/cost-model', json=model()).status_code == 403
        assert client.post('/orgs/other/meta-ads/campaigns/c1/analysis?start=2026-11-01&end=2026-11-11').status_code == 403
        assert client.get('/orgs/other/meta-ads/evaluation?start=2026-11-12&end=2026-11-01').status_code == 422
    finally:
        main.app.dependency_overrides.clear()


def test_ga4_richer_report_reuses_request_and_aggregates_existing_table(monkeypatch):
    statements, inserted, requests = [], [], []
    class Cursor:
        def execute(self, sql, params): statements.append((sql, params))
    @contextmanager
    def db(): yield Cursor()
    monkeypatch.setattr(commerce, 'database', db)
    monkeypatch.setattr(commerce, 'execute_values', lambda cur, sql, rows: inserted.extend(rows))
    monkeypatch.setattr(commerce, 'google_headers', lambda *a: {})
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        day = body['dateRanges'][0]['endDate'].replace('-', '')
        dims = [day, 'facebook', 'paid_social', 'dive']
        return httpx.Response(200, json={'metadata': {'timeZone': 'America/Toronto', 'currencyCode': 'CAD'}, 'rowCount': 2,
            'rows': [{'dimensionValues': [{'value': x} for x in dims+[path]],
                      'metricValues': [{'value': str(x)} for x in [2, 3, 1, 0, 0, 0]]}
                     for path in ['/products/promaster', '/products/other']]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        commerce.import_sessions(client, 'org', '123', 'America/Toronto', 14, detailed=True)
    assert len(requests) == 1
    assert 'hostName' not in [d['name'] for d in requests[0]['dimensions']]
    assert len(inserted) == 1
    assert inserted[0][-1].adapted['sessions'] == 4
    assert inserted[0][-1].adapted['itemViewEvents'] == 6
    snapshots = [p for sql, p in statements if 'marketing_evaluation_snapshots' in sql]
    assert len(snapshots[0][-1].adapted['rows']) == 2


def test_product_sync_scope_failure_preserves_snapshot(monkeypatch):
    monkeypatch.setattr(evaluation_data, 'shopify_query', lambda *a: {'currentAppInstallation': {'accessScopes': [{'handle': 'read_orders'}]}})
    with pytest.raises(commerce.ContentError, match='read_products'):
        evaluation_data.import_products(None, 'org', 'store', 'token')


def test_restricted_cost_access_still_imports_catalog(monkeypatch):
    calls, saved = [], []
    @contextmanager
    def db(): yield object()
    monkeypatch.setattr(evaluation_data, 'database', db)
    monkeypatch.setattr(evaluation_data, 'save_snapshot', lambda *a: saved.append(a))
    def query(client, shop, token, query, variables=None):
        if variables is None:
            return {'currentAppInstallation': {'accessScopes': [{'handle': 'read_products'}, {'handle': 'read_inventory'}]},
                    'shop': {'currencyCode': 'CAD'}}
        calls.append(variables)
        if variables['costs']:
            raise commerce.ContentError('Shopify API access denied.')
        return {'productVariants': {'nodes': [{'id': 'v'}], 'pageInfo': {'hasNextPage': False}}}
    monkeypatch.setattr(evaluation_data, 'shopify_query', query)
    assert evaluation_data.import_products(None, 'org', 'store', 'token') == 1
    assert [c['costs'] for c in calls] == [True, False]
    assert saved[0][4]['cost_access'] is False


def test_ga4_dimension_failure_falls_back_without_disabling_legacy_metrics(monkeypatch):
    from backend import content_jobs
    monkeypatch.setenv('COMMERCE_SYNC_ENABLED', 'true')
    monkeypatch.setenv('CAMPAIGN_EVALUATION_ENABLED', 'true')
    monkeypatch.setenv('CONTENT_GA4_PROPERTY_ID', '123')
    calls, statuses = [], []
    monkeypatch.setattr(commerce, 'import_orders', lambda *a, **k: 1)
    monkeypatch.setattr(evaluation_data, 'import_products', lambda *a: 2)
    def sessions(*args, detailed=False):
        calls.append(detailed)
        if detailed:
            raise commerce.ContentError('GA4 rejected report (INVALID_ARGUMENT).')
        return 3
    monkeypatch.setattr(commerce, 'import_sessions', sessions)
    monkeypatch.setattr(content_jobs, 'finish', lambda org, source, status, *a, **kw: statuses.append((source, status)) or status == 'failed')
    assert content_jobs.commerce_sync(None, 'org', 'store', 'token', '', '', 30)
    assert calls == [True, False]
    assert ('ga4_store', 'succeeded') in statuses
    assert ('ga4_landing', 'failed') in statuses


def test_sampled_ga4_report_never_replaces_snapshot(monkeypatch):
    monkeypatch.setattr(commerce, 'google_headers', lambda *a: {})
    def fail_db():
        raise AssertionError('Must not write incomplete data')
    monkeypatch.setattr(commerce, 'database', fail_db)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'metadata': {'samplingMetadatas': [{'samplesReadCount': '1'}]}}))) as client:
        with pytest.raises(commerce.ContentError, match='Previous import'):
            commerce.import_sessions(client, 'org', '123', 'America/Toronto', 14, detailed=True)


def test_campaign_panel_renders_and_cost_submission_uses_client_payload(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from frontend import campaign_evaluation as ui
    evidence = evaluate(fixture())
    monkeypatch.setattr(ui, 'load', lambda *a: evidence)
    calls = []
    monkeypatch.setattr(ui, 'call', lambda path, method='GET', **kw: calls.append((path, method, kw)) or {'saved': True})
    def panel():
        from frontend.campaign_evaluation import show
        show('/orgs/test', '2026-11-01', '2026-11-11', ['c1'])
    app = AppTest.from_function(panel).run()
    assert not app.exception
    next(w for w in app.text_input if w.label == 'Assumption source / effective date').set_value('Invoice 2026-11-12')
    app.checkbox[0].check()
    next(b for b in app.button if b.label == 'Save cost assumptions').click().run()
    assert not app.exception
    assert calls[0][1] == 'PUT'
    assert calls[0][2]['payload']['note'] == 'Invoice 2026-11-12'
