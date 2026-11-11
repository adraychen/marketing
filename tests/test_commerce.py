from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from backend import commerce
from backend.connectors.content import ContentError
from frontend.commerce import commerce_totals


def order(**changes):
    row = {'id':'gid://shopify/Order/1', 'createdAt':datetime.now(timezone.utc).isoformat(),
           'updatedAt':datetime.now(timezone.utc).isoformat(), 'test':False, 'cancelledAt':None,
           'displayFinancialStatus':'PAID',
           'currentTotalPriceSet':{'shopMoney':{'amount':'123.45','currencyCode':'CAD'},
                                   'presentmentMoney':{'amount':'90','currencyCode':'USD'}},
           'customerJourneySummary':{'ready':True,'lastVisit':{'utmParameters':{'source':'facebook','medium':'organic_social'}}}}
    return {**row, **changes}


@pytest.mark.parametrize('changes', [{'test':True}, {'cancelledAt':'2026-11-01'},
    {'displayFinancialStatus':'REFUNDED'}, {'displayFinancialStatus':'PENDING'}])
def test_non_sales_excluded(changes):
    assert not commerce.normalize_order(order(**changes))['eligible']


def test_money_and_explicit_attribution():
    row=commerce.normalize_order(order(displayFinancialStatus='PARTIALLY_REFUNDED'))
    assert row['eligible'] and row['amount']==Decimal('123.45')
    assert row['currency']=='CAD' and row['presentment']['currencyCode']=='USD'
    assert row['attributed'] and row['channel']=='Facebook Organic'
    for journey in (None, {'ready':False,'lastVisit':{'utmParameters':{'source':'facebook','medium':'organic_social'}}},
                    {'ready':True,'lastVisit':{'utmParameters':{'source':'facebook'}}}):
        assert not commerce.normalize_order(order(customerJourneySummary=journey))['attributed']


def test_scope_failure_does_not_touch_database(monkeypatch):
    monkeypatch.setattr(commerce,'shopify_query',lambda *a: {'currentAppInstallation':{'accessScopes':[]}})
    monkeypatch.setattr(commerce,'database',lambda: pytest.fail('must not write failed imports'))
    with pytest.raises(ContentError, match='read_orders'):
        commerce.import_orders(None,'org','store.myshopify.com','secret',90)


def test_order_pagination_deduplicates_before_atomic_write(monkeypatch):
    row=order(createdAt=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat())
    responses=iter([{'currentAppInstallation':{'accessScopes':[{'handle':'read_orders'}]},
                     'shop':{'ianaTimezone':'America/Toronto','currencyCode':'CAD'}},
        {'orders':{'nodes':[row], 'pageInfo':{'hasNextPage':True,'endCursor':'next'}}},
        {'orders':{'nodes':[deepcopy(row)], 'pageInfo':{'hasNextPage':False,'endCursor':None}}}])
    monkeypatch.setattr(commerce,'shopify_query',lambda *a: next(responses))
    statements=[]
    class Cursor:
        def execute(self,sql,params): statements.append(sql)
    @contextmanager
    def db(): yield Cursor()
    monkeypatch.setattr(commerce,'database',db)
    inserted=[]
    monkeypatch.setattr(commerce,'execute_values',lambda cur,sql,rows: inserted.extend(rows))
    assert commerce.import_orders(None,'org','store.myshopify.com','secret',90)==1
    assert len(inserted)==1
    assert any('delete from marketing_shopify_orders' in sql for sql in statements)
    assert any('marketing_commerce_coverage' in sql for sql in statements)


def test_restricted_ga4_preserves_previous_import(monkeypatch):
    monkeypatch.setattr(commerce,'google_headers',lambda *a:{})
    monkeypatch.setattr(commerce,'analytics_response',lambda response:{'metadata':{'subjectToThresholding':True}})
    monkeypatch.setattr(commerce,'database',lambda:pytest.fail('must preserve prior import'))
    class Client:
        def post(self,*a,**kw): return None
    with pytest.raises(ContentError,match='thresholded'):
        commerce.import_sessions(Client(),'org','123','America/Toronto',30)


def test_absent_commerce_does_not_become_zero():
    assert commerce_totals({'orders':[]})=={}
    assert commerce_totals({'orders':[{'currency':'CAD','purchases':0,'revenue':0,
        'attributed_purchases':0,'attributed_revenue':0}]})['CAD']['purchases']==0
