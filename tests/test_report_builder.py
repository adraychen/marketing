from datetime import date

from frontend import report_builder as builder
from frontend.reports import Report, filter_ads


def test_weekly_period_is_inclusive_and_separate_dates_are_gone():
    assert builder.report_period('Weekly report', week_end=date(2026,11,10)) == (date(2026,11,4),date(2026,11,10))
    assert builder.previous_period(date(2026,11,4), date(2026,11,10)) == (date(2026,10,28),date(2026,11,3))
    assert builder.report_period('Custom period',date(2026,10,12),date(2026,11,10)) == (date(2026,10,12),date(2026,11,10))


def test_campaign_exclusion_filters_all_descendants_and_metrics():
    snapshot = {'campaigns':[{'resource':'c1'},{'resource':'c2'}],
        'ad_groups':[{'resource':'g1','parent':'c1'},{'resource':'g2','parent':'c2'}],
        'ads':[{'resource':'a1','parent':'g1'},{'resource':'a2','parent':'g2'}],
        'keywords':[{'resource':'k1','parent':'g1'},{'resource':'k2','parent':'g2'}],
        'search_terms':[{'resource':'s1','parent':'g1'},{'resource':'s2','parent':'g2'}],
        'performance':[{'resource':key} for key in ('c1','c2','g1','g2','a1','a2','k1','k2','s1','s2')]}
    result = filter_ads(snapshot, ['c1'])
    assert {row['resource'] for row in result['performance']} == {'c1','g1','a1','k1','s1'}
    assert result['campaigns'] == [{'resource':'c1'}]
    assert filter_ads(snapshot, [])['performance'] == []
    assert len(snapshot['performance']) == 10


def test_content_scope_and_optional_sync(monkeypatch):
    catalog = {'items':[{'id':'p1','source':'facebook','campaigns':['topic']},
                        {'id':'p2','source':'facebook','campaigns':['topic']},
                        {'id':'blog','source':'shopify','campaigns':['topic']}],
               'campaigns':[{'slug':'topic'}], 'sources':[]}
    seen=[]
    def content(report, route, scoped, campaign, params, terms):
        seen.extend(item['id'] for item in scoped['items'])
        report.table('Content source status',[{'status':'succeeded'}])
        report.table('Content visibility diagnostics',[{'warning':'missing values'}])
        return {'p1':[]}
    monkeypatch.setattr(builder,'add_content',content)
    monkeypatch.setattr(builder,'add_facebook_analytics',lambda *a: (_ for _ in ()).throw(AssertionError('Page totals must not leak excluded posts')))
    monkeypatch.setattr(builder,'load_commerce',lambda *a: (_ for _ in ()).throw(AssertionError('Excluded commerce must not be fetched')))
    report=Report()
    builder.generate_period(report,'/orgs/o',catalog,[],['Facebook Organic'],{'facebook':['p1']},
        {'start':'2026-11-01','end':'2026-11-07'},date(2026,11,1),date(2026,11,7),False,False,False,False,False,[])
    assert seen == ['p1']
    assert 'Content source status' not in [name for name,rows in report.sections]
    assert 'Content visibility diagnostics' in [name for name,rows in report.sections]


def test_google_selection_and_optional_flags_reach_export(monkeypatch):
    seen=[]
    monkeypatch.setattr(builder,'add_ads',lambda *a,**kw: seen.append((a,kw)))
    report=Report()
    builder.generate_period(report,'/orgs/o',{'items':[],'campaigns':[]},[{'id':'a','name':'account'}],
        ['Google Ads'],{'google:a':['c1']},{'start':'2026-11-04','end':'2026-11-10'},
        date(2026,11,4),date(2026,11,10),True,True,True,False,True,[])
    args,kw=seen[0]
    assert args[7] is True and args[8] is True
    assert kw['selected']==['c1'] and kw['include_ai'] is False
    assert args[6]==date(2026,11,10)
