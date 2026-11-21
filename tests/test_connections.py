from unittest.mock import patch, MagicMock
import pytest
from fastapi import HTTPException
from frontend.connections import run_selected, fetch
from frontend.client import APIError


def test_selected_sync_continues_after_failure_and_skips_unconnected():
    targets = {'Google Ads': [('/accounts/a/sync', None)], 'Shopify': [('/content/sync', {'source': 'shopify'})]}
    with patch('frontend.connections.call', side_effect=[APIError('Unavailable'), {'status': 'partial'}]) as call:
        results = run_selected('/orgs/a', ['Google Ads', 'Shopify', 'TikTok Ads'], targets)
    assert [r['Result'] for r in results] == ['Failed', 'partial', 'Skipped']
    assert call.call_count == 2
    assert call.call_args.kwargs['params'] == {'source': 'shopify'}


def test_all_platform_status_includes_content_and_paid_counts():
    def read(path):
        if path.endswith('/accounts'): return [{'id': 'a', 'name': 'Google'}]
        if path.endswith('/accounts/a/sync'): return [{'status': 'succeeded', 'counts': {'campaigns': 4}}]
        if path.endswith('/content'): return {'sources': [{'source': 'facebook', 'status': 'succeeded', 'records': 9}], 'items': []}
        return {'configured': True, 'runs': [{'status': 'succeeded', 'counts': {'daily_rows': 3}}]}
    with patch('frontend.connections.call', side_effect=read):
        rows, targets = fetch('/orgs/a')
    assert any(r['Platform'] == 'Meta Organic Ads' and r.get('Records') == 9 for r in rows)
    assert any(r['Platform'] == 'Reddit Ads' and r.get('Records') == 3 for r in rows)
    assert len({r['Platform'] for r in rows}) == 7
    assert targets['Meta Organic Ads'][0][1] == {'source': 'facebook'}


def test_content_endpoint_rejects_nonadmin_and_wrong_org():
    from backend.main import sync_content_selected
    with patch('backend.main.MODE', 'live'), patch('backend.content_jobs.environment', return_value='other'):
        with pytest.raises(HTTPException) as exc:
            sync_content_selected('org', 'facebook', {'role': 'viewer'})
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            sync_content_selected('org', 'facebook', {'role': 'admin'})
        assert exc.value.status_code == 403


def test_selected_content_job_does_not_sync_other_platforms():
    from backend import content_jobs as jobs
    env = {'CONTENT_ORGANIZATION_ID': '00000000-0000-0000-0000-000000000001'}
    def environment(key, default=''): return env.get(key, default)
    db = MagicMock()
    db.return_value.__enter__.return_value.fetchone.return_value = {'acquired': True}
    with patch.object(jobs, 'MODE', 'live'), patch.object(jobs, 'environment', side_effect=environment), \
         patch.object(jobs, 'database', db), patch.object(jobs.repo, 'associate'), \
         patch.object(jobs, 'discovery', return_value=False) as discover, \
         patch.object(jobs, 'post_insights', return_value=False), patch.object(jobs, 'page_insights', return_value=False), \
         patch.object(jobs, 'organic_destinations', return_value=False), \
         patch.object(jobs, 'commerce_sync') as commerce, patch.object(jobs, 'report_items') as reports:
        assert jobs.main([], platforms={'facebook'}) == 0
    assert discover.call_count == 1
    assert discover.call_args.args[2] == 'facebook'
    commerce.assert_not_called()
    reports.assert_not_called()
