"""Organization-wide sync controls; each request retains the backend's access checks."""
import streamlit as st
from frontend.client import APIError, call
from frontend.views import table, pretty, sync_result, CONTENT_SYNC_SOURCES

PAID = {'Meta Paid Ads': 'meta-ads', 'ChatGPT Ads': 'chatgpt-ads', 'Reddit Ads': 'reddit-ads'}
PLATFORMS = ['Google Ads', 'Meta Organic Ads', 'Meta Paid Ads', 'Shopify', 'TikTok Ads', 'ChatGPT Ads', 'Reddit Ads']


def fetch(base):
    rows, targets = [], {}
    def read(path):
        try:
            return call(base + path)
        except APIError as exc:
            rows.append({'Platform': path, 'Result': 'Status unavailable', 'Details': str(exc)})
            return None
    def runs(platform, history, account=None):
        if not history:
            rows.append({'Platform': platform, 'Account': account, 'Result': 'No run recorded'})
            return
        latest = history[0]
        success = next((r.get('finished_at') for r in history if r.get('status') == 'succeeded'), None)
        counts = latest.get('counts') or {'Imported records': latest.get('records')}
        for data, count in counts.items():
            rows.append({'Platform': platform, 'Account': account, 'Data': pretty(data),
                         'Result': ('Succeeded · count unavailable' if latest.get('status') == 'succeeded' and count is None
                                    else sync_result(latest.get('status'), count, latest.get('message') or latest.get('error'))),
                         'Records': count, 'Last run': latest.get('finished_at') or latest.get('created_at'),
                         'Last success (available history)': success,
                         'Details': latest.get('message') or latest.get('error')})
    accounts = read('/accounts') or []
    targets['Google Ads'] = [(f"/accounts/{a['id']}/sync", None) for a in accounts]
    if not accounts:
        rows.append({'Platform': 'Google Ads', 'Result': 'Not connected'})
    for account in accounts:
        history = read(f"/accounts/{account['id']}/sync")
        if history is not None:
            runs('Google Ads', history, account['name'])
    for platform, slug in PAID.items():
        status = read(f'/{slug}/status')
        if status is None:
            continue
        ready = status.get('configured') and status.get('schema_ready', True) and status.get('authorized', True)
        targets[platform] = [(f'/{slug}/sync', None)] if ready else []
        if not ready:
            rows.append({'Platform': platform, 'Result': 'Setup required',
                         'Details': 'Check connection, authorization and database migrations.'})
        runs(platform, status.get('runs', []))
        if status.get('token_expiring'):
            rows.append({'Platform': platform, 'Result': 'Token renewal needed',
                         'Details': status.get('token_expires_at') or 'Check token expiry on the API service.'})
    catalog = read('/content')
    if catalog is not None:
        sources = {r['source']: r for r in catalog.get('sources', [])}
        for platform, old in [('Meta Organic Ads', 'Facebook'), ('Shopify', 'Shopify')]:
            source, labels = CONTENT_SYNC_SOURCES[old]
            labels = dict(labels)
            if source == 'facebook':
                labels['ga4_organic_destinations'] = 'GA4 destination performance'
            else:
                labels.update(ga4_channels='GA4 channel attribution', ga4_landing='GA4 landing performance', shopify_products='Products')
            targets[platform] = [('/content/sync', {'source': source})]
            for key, label in labels.items():
                r = sources.get(key, {})
                rows.append({'Platform': platform, 'Data': label,
                             'Result': sync_result(r.get('status'), r.get('records'), r.get('message')),
                             'Records': r.get('records'), 'Last run': r.get('finished_at'),
                             'Last success (available history)': r.get('last_success_at'), 'Details': r.get('message')})
    tiktok = read('/tiktok-ads/status')
    if tiktok is not None:
        rows.append({'Platform': 'TikTok Ads', 'Result': 'On-demand report only' if tiktok.get('configured') else 'Not connected',
                     'Details': 'No persistent sync history. Load reports from Paid Advertising → TikTok Ads.'})
    return rows, targets


def run_selected(base, selected, targets):
    results = []
    for platform in selected:
        jobs = targets.get(platform, [])
        if not jobs:
            results.append({'Platform': platform, 'Result': 'Skipped', 'Details': 'Not connected, setup incomplete, or on-demand reporting only.'})
        for path, params in jobs:
            try:
                response = call(base + path, 'POST', params=params, timeout=3600)
                results.append({'Platform': platform, 'Target': path, 'Result': response.get('status', 'Completed'),
                                'Details': response.get('message')})
            except APIError as exc:
                results.append({'Platform': platform, 'Target': path, 'Result': 'Failed', 'Details': str(exc)})
    return results


def show(mode, org, base):
    st.title('Connections & Sync')
    st.caption(f"{org['name']} · {pretty(org['role'])}")
    rows, targets = fetch(base)
    all_platforms = st.checkbox('Select all platforms', value=True, key=f'all-sync:{base}')
    selected = PLATFORMS if all_platforms else st.multiselect('Platforms to sync', PLATFORMS, key=f'selected-sync:{base}')
    st.caption('Meta Organic Ads collects Facebook organic posts and Page insights; it does not include paid ads or an Instagram connection. Google Ads sync includes all connected Google accounts.')
    if st.button('Sync selected platforms', type='primary', disabled=mode != 'live' or org['role'] != 'admin' or not selected):
        with st.spinner('Syncing selected platforms… Results will appear below.'):
            st.session_state[f'sync-results:{base}'] = run_selected(base, selected, targets)
        rows, targets = fetch(base)
    if org['role'] != 'admin':
        st.caption('Only organization admins can run syncs or connect accounts.')
    if mode != 'live':
        st.info('Demo mode: live synchronization is disabled.')
    results = st.session_state.get(f'sync-results:{base}')
    if results:
        st.subheader('Last manual sync request')
        table(results)
    st.subheader('Latest sync — all platforms')
    st.caption('Latest stored results, including scheduled runs. Record counts describe imports, not marketing outcomes. Times are shown as recorded, with timezone offsets.')
    table(rows, download_name='all-platform-sync-status.csv')
    if org['role'] == 'admin' and mode == 'live':
        with st.expander('Manage connections · Google Ads accounts'):
            st.caption('Use this only to discover and connect another Google Ads account. Existing accounts do not need reconnecting.')
            if st.button('Discover accessible accounts'):
                st.session_state[f'discovery:{base}'] = call(f'{base}/discovery', timeout=90)
            options = st.session_state.get(f'discovery:{base}')
            if options:
                mapping = {r['resource']: r for r in options}
                selected_account = st.selectbox('Available Google Ads account', list(mapping), format_func=lambda k: f"{mapping[k]['name']} · {k}")
                if st.button('Connect account'):
                    call(f'{base}/accounts', 'POST', params={'resource': selected_account}, timeout=90)
                    st.rerun()
            elif options is not None:
                st.info('No accessible Google Ads accounts were returned.')
