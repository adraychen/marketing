from frontend.meta_ads import campaign_option_label, scoped


def test_same_name_campaigns_have_distinct_status_and_id_labels():
    archived = dict(id='old', name='Black Friday', effective_status='ARCHIVED')
    active = dict(id='new', name='Black Friday', effective_status='ACTIVE')
    assert campaign_option_label(archived) == 'Black Friday · ARCHIVED · old'
    assert campaign_option_label(active) == 'Black Friday · ACTIVE · new'
    data = dict(campaigns=[archived, active], adsets=[], ads=[], daily=[
        dict(campaign_id='new', ad_id='ad', day='2026-11-20', spend=42.24, impressions=2020)])
    assert scoped(data, ['old'])['daily'] == []
    assert scoped(data, ['new'])['daily'][0]['spend'] == 42.24


def test_active_campaigns_excludes_archived_paused_and_unknown():
    from frontend.meta_ads import active_campaigns
    data = {'campaigns': [
        {'id': 'active', 'effective_status': 'ACTIVE'},
        {'id': 'archived', 'effective_status': 'ARCHIVED', 'status': 'ACTIVE'},
        {'id': 'paused', 'effective_status': 'PAUSED'},
        {'id': 'unknown'},
        {'id': 'fallback', 'status': 'ACTIVE'}]}
    assert [r['id'] for r in active_campaigns(data)] == ['active', 'fallback']
    assert active_campaigns({'campaigns': []}) == []
