from contextlib import contextmanager
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import auth, main, meta_ads
from backend.connectors import meta_ads as connector
from frontend import meta_ads as ui


def configure(monkeypatch):
    monkeypatch.setenv('META_ADS_ORGANIZATION_ID','tenant-a')
    monkeypatch.setenv('META_ADS_ACCOUNT_ID','2966294073730346')
    monkeypatch.setenv('META_ADS_ACCESS_TOKEN','super-secret-token')
    monkeypatch.setenv('META_ADS_API_VERSION','v26.0')
    monkeypatch.delenv('META_ADS_TOKEN_EXPIRES_AT',raising=False)
    return connector.configuration('tenant-a')


def test_configuration_is_tenant_scoped_and_expiry_is_explicit(monkeypatch):
    assert configure(monkeypatch)['token_expires_at'] is None
    assert connector.configuration('tenant-b') is None
    monkeypatch.setenv('META_ADS_TOKEN_EXPIRES_AT','2027-01-08T00:00:00Z')
    assert connector.configuration('tenant-a')['token_expires_at']=='2027-01-08T00:00:00+00:00'
    monkeypatch.setenv('META_ADS_TOKEN_EXPIRES_AT','2027-01-08')
    with pytest.raises(connector.MetaAdsError,match='ISO timestamp'):
        connector.configuration('tenant-a')


def test_reader_uses_only_get_and_rebuilds_pagination_on_trusted_host(monkeypatch):
    config=configure(monkeypatch)
    requests=[]
    def transport(request):
        requests.append(request)
        assert request.method=='GET'
        assert request.headers['authorization']=='Bearer super-secret-token'
        assert request.url.host=='graph.facebook.com'
        if 'after' not in request.url.params:
            return httpx.Response(200,json={'data':[{'id':'one'}],'paging':{'next':'https://evil.example/steal?access_token=super-secret-token','cursors':{'after':'next-page'}}})
        return httpx.Response(200,json={'data':[{'id':'two'}]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        rows=list(connector.Reader(client,config).rows('act_2966294073730346/ads',{'fields':'id'}))
    assert [row['id'] for row in rows]==['one','two']
    assert all('super-secret-token' not in str(request.url) for request in requests)


@pytest.mark.parametrize('code,status,message',[(190,400,'expired'),(200,403,'ads_read'),(613,429,'rate limit')])
def test_upstream_errors_are_sanitized(monkeypatch,code,status,message):
    config=configure(monkeypatch)
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(status,json={'error':{'code':code,'message':'super-secret-token'}}))) as client:
        with pytest.raises(connector.MetaAdsError,match=message) as failure:
            connector.Reader(client,config).get('act_2966294073730346')
    assert 'super-secret-token' not in str(failure.value)


def test_actions_are_not_double_counted_and_absent_is_not_zero():
    raw={'ad_id':'a1','adset_id':'g1','campaign_id':'c1','date_start':'2026-11-10','actions':[{'action_type':'purchase','value':'5'},
        {'action_type':'omni_purchase','value':'5'},{'action_type':'offsite_conversion.fb_pixel_purchase','value':'3'}],
         'action_values':[{'action_type':'offsite_conversion.fb_pixel_purchase','value':'123.45'}]}
    normalized=connector.normalize_metric(raw)
    assert normalized['website_purchases']==3
    assert normalized['website_purchase_value']==123.45
    assert connector.normalize_metric({'ad_id':'a1','adset_id':'g1','campaign_id':'c1','date_start':'2026-11-10'})['website_purchases'] is None
    assert normalized['spend'] is None


def test_import_failure_does_not_delete_previous_data(monkeypatch):
    configure(monkeypatch)
    statements=[]
    class Cursor:
        def execute(self,sql,params): statements.append(sql)
        def fetchone(self): return {'acquired':True}
    @contextmanager
    def db(): yield Cursor()
    monkeypatch.setattr(meta_ads,'database',db)
    monkeypatch.setattr(meta_ads.Reader,'get',lambda *a,**kw:{'account_id':'2966294073730346','timezone_name':'America/Toronto'})
    monkeypatch.setattr(meta_ads,'fetch',lambda *a: (_ for _ in ()).throw(connector.MetaAdsError('access denied')))
    with pytest.raises(connector.MetaAdsError):
        meta_ads.run_sync('tenant-a')
    assert not any('delete' in sql.lower() for sql in statements)
    assert any('marketing_meta_ads_sync_runs' in sql for sql in statements)


def test_campaign_scope_and_nonadditive_reach():
    data={'coverage':True,'campaigns':[{'id':'1','name':'Keep'},{'id':'2','name':'Exclude'}],
          'adsets':[{'id':'g1','campaign_id':'1'},{'id':'g2','campaign_id':'2'}],
          'ads':[{'id':'a1','campaign_id':'1'},{'id':'a2','campaign_id':'2'}],
          'daily':[{'campaign_id':'1','spend':10,'reach':8,'impressions':20,'clicks':2,'inline_link_clicks':1,'website_purchases':None},
                   {'campaign_id':'2','spend':50,'reach':12,'impressions':100,'clicks':10}]}
    selected=ui.scoped(data,['1'])
    assert len(selected['ads'])==1
    assert ui.totals(selected)['spend']==10
    assert ui.totals(selected)['website_purchases'] is None
    assert 'reach' not in ui.totals(data)
    assert ui.totals({'coverage':False,'daily':[]})['spend'] is None
    assert ui.totals({'coverage':True,'daily':[]})['spend']==0


def test_meta_routes_require_membership_admin_and_valid_dates(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(main,'MODE','live')
    monkeypatch.setattr(auth,'MODE','live')
    monkeypatch.setattr(main.repo,'memberships',lambda user:[{'id':'tenant-a','role':'member'}])
    monkeypatch.setattr(auth,'auth_call',lambda *a,**kw:{'id':'6fab9887-3954-4dce-83e2-3c445024cda1'})
    monkeypatch.setattr(meta_ads,'status',lambda org:{'configured':True,'account_id':'2966294073730346'})
    client=TestClient(main.app)
    headers={'Authorization':'Bearer unit-test'}
    assert client.get('/orgs/tenant-a/meta-ads/status').status_code==401
    assert client.get('/orgs/tenant-b/meta-ads/status',headers=headers).status_code==404
    status=client.get('/orgs/tenant-a/meta-ads/status',headers=headers)
    assert status.status_code==200 and 'super-secret-token' not in status.text
    assert client.post('/orgs/tenant-a/meta-ads/sync',headers=headers).status_code==403
    assert client.get('/orgs/tenant-a/meta-ads/report',params={'start':'2026-11-10','end':'2026-11-01'},headers=headers).status_code==422


def test_fetch_imports_settings_creative_and_daily_rows(monkeypatch):
    import json
    config=configure(monkeypatch)
    def transport(request):
        path=request.url.path
        if path.endswith('/campaigns'):
            assert 'PENDING_REVIEW' not in json.loads(request.url.params['effective_status'])
            return httpx.Response(200,json={'data':[{'id':'c1','name':'Campaign','daily_budget':'1000'}]})
        if path.endswith('/adsets'):
            return httpx.Response(200,json={'data':[{'id':'g1','campaign_id':'c1'}]})
        if path.endswith('/ads'):
            return httpx.Response(200,json={'data':[{'id':'a1','campaign_id':'c1','adset_id':'g1','creative':{'body':'Review this ad'}}]})
        if path.endswith('/insights'):
            assert request.url.params['action_report_time']=='impression'
            assert request.url.params['use_unified_attribution_setting']=='true'
            return httpx.Response(200,json={'data':[{'ad_id':'a1','campaign_id':'c1','adset_id':'g1',
                'date_start':'2026-11-10','date_stop':'2026-11-10','spend':'12.34','impressions':'100','clicks':'2'}]})
        return httpx.Response(200,json={'account_id':'2966294073730346','currency':'CAD','timezone_name':'America/Toronto'})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        account,resources,metrics=connector.fetch(client,config,date(2026,11,10),date(2026,11,10))
    assert metrics[0]['spend']==12.34
    assert resources['ads'][0]['creative']['body']=='Review this ad'
    assert resources['campaigns'][0]['daily_budget']=='1000'


@pytest.mark.skipif(not __import__('os').getenv('TEST_DATABASE_URL'),reason='requires migrated Supabase test database')
def test_paid_postgres_repeat_import_and_rls():
    import os
    from uuid import uuid4
    import psycopg2
    from psycopg2.extras import RealDictCursor
    org,other,user=(str(uuid4()) for _ in range(3))
    conn=psycopg2.connect(os.environ['TEST_DATABASE_URL'])
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("insert into marketing_organizations(id,name) values(%s,'Paid test A'),(%s,'Paid test B')",(org,other))
            cur.execute('insert into auth.users(id) values(%s)',(user,))
            cur.execute('insert into marketing_memberships(organization_id,user_id) values(%s,%s)',(org,user))
            config={'account_id':'123','version':'v26.0','token_expires_at':None}
            account={'account_id':'123','name':'Test','currency':'CAD','timezone_name':'America/Toronto'}
            resources={'campaigns':[{'id':'c1','name':'Campaign'}],'adsets':[],'ads':[]}
            row={'ad_id':'a1','campaign_id':'c1','adset_id':'g1','day':'2026-11-10','spend':10}
            for tenant in (org,org,other):
                meta_ads.write_import(cur,tenant,config,account,resources,[row],date(2026,11,10),date(2026,11,10))
            cur.execute('select count(*) from marketing_meta_ads_daily where organization_id=%s',(org,))
            assert cur.fetchone()['count']==1
            cur.execute('set local role authenticated')
            cur.execute("select set_config('request.jwt.claim.sub',%s,true)",(user,))
            for table in ('marketing_meta_ads_accounts','marketing_meta_ads_resources','marketing_meta_ads_daily'):
                cur.execute(f'select count(*) from {table} where organization_id in (%s,%s)',(org,other))
                assert cur.fetchone()['count']==1
    finally:
        conn.rollback()
        conn.close()
