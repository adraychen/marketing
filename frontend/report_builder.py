"""A single, explicit scope for report inputs, preview, and export."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from backend.metrics import previous_period
from frontend.client import APIError, call
from frontend.commerce import load_commerce
from frontend.meta_ads import add_meta_report
from frontend.reports import Report, add_ads, add_content, add_facebook_analytics
from frontend.tiktok_ads import TIKTOK_NOTE, campaign_totals

SOURCES = ('Google Ads', 'Facebook Organic', 'Facebook Paid Ads', 'Shopify Blog',
           'Shopify Store Results', 'TikTok Ads')
SYNC_TABLES = {'Google Ads sync history', 'Content source status', 'Commerce latest sync results',
               'Facebook Data Availability and API Limitations'}


def report_period(kind, start=None, end=None, week_end=None):
    if kind == 'Weekly report':
        return week_end - timedelta(days=6), week_end
    return start, end


def item_checks(label, options, prefix):
    """Stable keys retain exclusions across date changes and select new items by default."""
    with st.expander(f'{label} ({len(options)})', expanded=True):
        if not options:
            st.caption('No imported campaigns or content available.')
            return []
        def set_all(value):
            for identifier, _ in options:
                st.session_state[f'{prefix}:{identifier}'] = value
        left, right = st.columns(2)
        left.button('Select all', key=f'{prefix}:all', on_click=set_all, args=(True,))
        right.button('Clear all', key=f'{prefix}:none', on_click=set_all, args=(False,))
        selected = []
        for identifier, name in options:
            key = f'{prefix}:{identifier}'
            if key not in st.session_state:
                st.session_state[key] = True
            if st.checkbox(name, key=key):
                selected.append(identifier)
        return selected


def trim_optional(report, sync_details):
    if not sync_details:
        report.sections = [(name, rows) for name, rows in report.sections if name not in SYNC_TABLES]


def generate_period(report, base, catalog, accounts, selected, choices, params, start, end,
                    weekly, compare, drafts, ai, sync_details, tiktok_rows):
    if 'Shopify Store Results' in selected and choices.get('store'):
        commerce = load_commerce(base, start, end)
        report.note(commerce.get('policy', commerce.get('error', 'Commerce unavailable.')))
        report.note(commerce.get('attribution_model', 'Order attribution unavailable.'))
        report.table('Commerce source coverage', commerce['coverage'])
        report.table('Commerce latest sync results', commerce.get('latest_runs', []))
        report.table('Shopify canonical order outcomes by channel', commerce['orders'])
        report.table('Storewide GA4 sessions and purchase outcomes', commerce['ga4'])
    if 'Google Ads' in selected:
        for account in accounts:
            ids = choices.get(f"google:{account['id']}", [])
            if not ids:
                continue
            report.note(f"Google Ads account: {account['name']} ({account['id']}).")
            add_ads(report, account, f"{base}/accounts/{account['id']}", params, start, end,
                    end if weekly and compare else None, True, drafts,
                    selected=ids, include_ai=ai, compare=False)
        if ai and not (weekly and compare):
            report.note('AI interpretation unavailable for this scope. Existing Google Ads AI results require '
                        'a weekly report with comparison. No new AI request is made by exporting.')
    ids = set(choices.get('facebook', []) + choices.get('shopify', []))
    items = [item for item in catalog['items'] if item['id'] in ids]
    if items:
        slugs = {slug for item in items for slug in item.get('campaigns', [])}
        scoped_catalog = {**catalog, 'items': items,
                          'campaigns': [row for row in catalog['campaigns'] if row['slug'] in slugs]}
        snapshots = add_content(report, f'{base}/content', scoped_catalog, None, params, True)
        # Associated ad campaigns are context, not extra report selections.
        report.sections = [(name, rows) for name, rows in report.sections if name != 'Linked Google Ads campaigns']
        selected_fb = choices.get('facebook', [])
        if not selected_fb:
            report.sections = [(name, rows) for name, rows in report.sections
                               if not name or not name.startswith('Facebook')]
        if not choices.get('shopify'):
            report.sections = [(name, rows) for name, rows in report.sections
                               if not name or not name.startswith(('Shopify article', 'Google Search'))]
        all_fb = [item for item in catalog['items'] if item['source'] == 'facebook']
        if selected_fb and len(selected_fb) == len(all_fb):
            add_facebook_analytics(report, f'{base}/content', params, snapshots, True)
        elif selected_fb:
            report.note('Facebook Page-wide totals omitted because only some posts were selected. '
                        'Post metrics cover the selected posts only.')
        if ai:
            report.note('AI interpretation unavailable for organic content in this export. '
                        'Metrics and content details are provided for further analysis.')
    if 'Facebook Paid Ads' in selected and choices.get('meta'):
        add_meta_report(report,base,start,end,choices['meta'])
        if sync_details:
            report.table('Meta paid sync history',call(f'{base}/meta-ads/status')['runs'])
        if ai:
            report.note('Meta paid AI interpretation is not generated by exporting. Paid metrics and creative details are provided for further analysis.')
    if 'TikTok Ads' in selected and choices.get('tiktok'):
        rows = [row for row in tiktok_rows if str(row['campaign_id']) in choices['tiktok']]
        report.note(TIKTOK_NOTE)
        report.table('TikTok Ads campaign totals', campaign_totals(rows))
        report.table('TikTok Ads daily campaign performance', rows)
        if ai:
            report.note('TikTok AI interpretation is unavailable in this export.')
    trim_optional(report, sync_details)


def show_report_builder(org, base, mode):
    st.title('Reports & AI Insights')
    st.caption('Choose one period, sources, and items. Performance, settings, and content details are included automatically.')
    prefix = f"report:{org['id']}"
    last = datetime.now(ZoneInfo('America/Toronto')).date() - timedelta(days=1)
    st.subheader('1. Reporting period')
    kind = st.radio('Report type', ('Custom period', 'Weekly report'), horizontal=True, key=f'{prefix}:kind')
    if kind == 'Weekly report':
        ending = st.date_input('Week ending', last, min_value=date(2000, 1, 14), max_value=last,
                               key=f'{prefix}:ending')
        start, end = report_period(kind, week_end=ending)
    else:
        dates = st.date_input('Report date range', (last - timedelta(days=29), last),
                              min_value=date(2000, 1, 14), max_value=last, key=f'{prefix}:dates')
        if len(dates) != 2:
            st.info('Select both report dates.')
            return
        start, end = report_period(kind, *dates)
    if end < start or (end-start).days > 365:
        st.warning('Choose a period of up to 366 days.')
        return
    compare = st.checkbox('Compare with previous period', value=True, key=f'{prefix}:compare')
    prior_start, prior_end = previous_period(start, end)
    st.caption(f'Report: {start} to {end} (inclusive).')
    if compare:
        st.caption(f'Comparison: {prior_start} to {prior_end}. All selected sources use these periods, '
                   'with their own reporting timezones and data delays.')
    params = {'start': str(start), 'end': str(end)}
    warnings = []
    try:
        catalog = call(f'{base}/content')
    except APIError:
        catalog = {'items': [], 'campaigns': [], 'sources': []}
        warnings.append('Content catalog unavailable. Check Connections & Sync.')
    try:
        accounts = call(f'{base}/accounts')
    except APIError:
        accounts = []
        warnings.append('Google Ads accounts unavailable.')
    try:
        tiktok_ready = mode == 'live' and call(f'{base}/tiktok-ads/status')['configured']
    except APIError:
        tiktok_ready = False
    try:
        meta_status = call(f'{base}/meta-ads/status')
    except APIError:
        meta_status = {'configured':False, 'accounts':[], 'runs':[]}
    statuses = {'Google Ads': 'Connected' if accounts else 'Not connected',
                'Facebook Organic': 'Imported content available' if any(i['source']=='facebook' for i in catalog['items']) else 'No imported posts',
                'Shopify Blog': 'Imported content available' if any(i['source']=='shopify' for i in catalog['items']) else 'No imported articles',
                'Facebook Paid Ads': 'Imported paid data available' if meta_status['accounts'] else 'Configured; no successful import yet' if meta_status['configured'] else 'Not connected', 'Shopify Store Results': 'Coverage checked on generation',
                'TikTok Ads': 'Configured; connection checked when selected' if tiktok_ready else 'Not connected'}
    st.subheader('2. Sources to include')
    selected = [source for source in SOURCES if st.checkbox(source, value=source != 'TikTok Ads' and (source != 'Facebook Paid Ads' or bool(meta_status['accounts'])),
                 key=f'{prefix}:source:{source}', help=statuses[source])]
    st.caption('Connection status is separate from selection. Unavailable selected sources will be flagged, never reported as zero.')
    st.dataframe([{'Source': s, 'Status': statuses[s]} for s in SOURCES], hide_index=True)
    st.subheader('3. Campaigns and content')
    st.caption('All available items start selected. Only checked items from checked sources appear in the report.')
    choices = {}
    for source in selected:
        if source == 'Google Ads':
            for account in accounts:
                try:
                    snapshot = call(f"{base}/accounts/{account['id']}/report", params=params)
                    options = [(row['resource'], row['data'].get('name') or row['resource']) for row in snapshot['campaigns']]
                    choices[f"google:{account['id']}"] = item_checks(f"Google Ads / {account['name']}", options, f"{prefix}:google:{account['id']}")
                except APIError:
                    warnings.append(f"Google Ads campaigns unavailable for {account['name']}.")
            if not accounts:
                warnings.append('Google Ads selected but no account is connected.')
        elif source in ('Facebook Organic', 'Shopify Blog'):
            platform = 'facebook' if source == 'Facebook Organic' else 'shopify'
            options = [(item['id'], item['title']) for item in catalog['items'] if item['source']==platform]
            choices[platform] = item_checks(source, options, f'{prefix}:{platform}')
            if not options:
                warnings.append(f'{source} selected but no imported content is available.')
        elif source == 'Shopify Store Results':
            choices['store'] = item_checks(source, [('store', 'Storewide orders, revenue, sessions, and attribution')], f'{prefix}:store')
            st.caption('Storewide outcomes cover the whole store. They are separate from campaign and article metrics.')
        elif source == 'Facebook Paid Ads':
            choices['meta'] = []
            if meta_status.get('token_expiring'):
                warnings.append('Meta paid token expiry is within seven days or has passed. Renew it in Render.')
            elif meta_status.get('configured') and not meta_status.get('token_expires_at'):
                warnings.append('Meta paid token expiry is unknown. Check its expiry in Meta and record it in Render.')
            try:
                paid = call(f'{base}/meta-ads/report',params=params)
                choices['meta'] = item_checks(source,[(row['id'],row.get('name') or row['id']) for row in paid['campaigns']],f'{prefix}:meta')
                if not paid['account']:
                    warnings.append('Facebook Paid Ads selected but there is no successful paid import yet.')
                elif not paid['coverage'] or paid['stale']:
                    warnings.append('Meta paid data is partial or stale for these dates. Coverage is included in the report.')
                if meta_status['runs'] and meta_status['runs'][0]['status']=='failed':
                    warnings.append('Latest Meta paid sync failed. The report uses the last successful import.')
            except APIError:
                warnings.append('Meta paid report is unavailable. Check its migration and Connections & Sync.')
        elif source == 'TikTok Ads':
            choices['tiktok'] = []
            if not tiktok_ready:
                warnings.append('TikTok Ads selected but the advertiser connection is unavailable.')
            else:
                try:
                    rows = call(f'{base}/tiktok-ads/report', params=params, timeout=90)['rows']
                    labels = {str(row['campaign_id']): row['campaign_name'] for row in rows}
                    choices['tiktok'] = item_checks(source, list(labels.items()), f'{prefix}:tiktok')
                    if not labels:
                        warnings.append('TikTok returned no campaigns for these dates. This does not establish zero spend.')
                except APIError:
                    warnings.append('TikTok advertiser data could not be retrieved. No TikTok metrics will be included.')
    st.subheader('4. Optional report details')
    drafts = st.checkbox('Include saved ad drafts', key=f'{prefix}:drafts', help='Saved Google Ads revisions where supported. These are not published ads.')
    ai = st.checkbox('Include AI interpretations and recommendations', value=True, key=f'{prefix}:ai',
                     help='Includes existing interpretations that match the selected scope. Missing AI results are flagged. Exporting does not call AI.')
    sync_details = st.checkbox('Include sync status and data availability details', value=True, key=f'{prefix}:sync')
    st.caption('All available performance metrics, campaign settings, search detail, hashtags, SEO titles, and meta descriptions '
               'are included automatically. Missing-data warnings remain visible even when detailed sync history is excluded.')
    st.subheader('5. Review and generate')
    st.write(f'{kind}: {start} to {end}')
    st.write('Sources: ' + (', '.join(selected) or 'None selected'))
    groups = {'facebook':'Facebook Organic posts', 'shopify':'Shopify Blog articles',
              'store':'Shopify Store Results', 'tiktok':'TikTok Ads campaigns', 'meta':'Meta paid campaigns',
              **{f"google:{a['id']}": f"Google Ads / {a['name']}" for a in accounts}}
    st.dataframe([{'Group': groups.get(key, key), 'Selected items': len(ids)}
                  for key, ids in choices.items()], hide_index=True)
    st.write('Optional details: ' + (', '.join(label for label, enabled in
             [('Saved drafts', drafts), ('AI interpretations', ai), ('Sync details', sync_details)] if enabled) or 'None'))
    if compare:
        st.write(f'Previous period: {prior_start} to {prior_end}')
    for warning in warnings:
        st.warning(warning)
    scope = repr((org['id'], kind, start, end, compare, selected, choices, drafts, ai, sync_details, warnings))
    if st.button('Generate report', type='primary', disabled=not selected):
        st.session_state.pop(f'{prefix}:generated', None)
        try:
            with st.spinner('Preparing selected report...'):
                report = Report()
                report.table('Report scope', [{'organization':org['name'], 'mode':mode, 'report_type':kind,
                    'start':str(start), 'end':str(end), 'comparison':compare, 'sources':selected,
                    'selected_items':choices, 'saved_drafts':drafts, 'ai_interpretations':ai,
                    'sync_details':sync_details, 'generated_at_utc':datetime.now(timezone.utc).isoformat()}])
                report.table('Coverage warnings', [{'warning':warning} for warning in warnings])
                for period_start, period_end, prior in [(start,end,False)] + ([(prior_start,prior_end,True)] if compare else []):
                    output = Report()
                    tiktok_rows = []
                    if choices.get('tiktok'):
                        tiktok_rows = call(f'{base}/tiktok-ads/report', params={'start':str(period_start),'end':str(period_end)}, timeout=90)['rows']
                    generate_period(output,base,catalog,accounts,selected,choices,
                        {'start':str(period_start),'end':str(period_end)},period_start,period_end,
                        kind=='Weekly report' and not prior,compare and not prior,
                        drafts and not prior,ai and not prior,sync_details,tiktok_rows)
                    report.sections.extend((('Previous period / '+name if prior and name else name), rows)
                                           for name,rows in output.sections)
                st.session_state[f'{prefix}:generated'] = scope, report.export(), [
                    {'Section':name,'Rows':len(rows)} for name,rows in report.sections if name]
        except APIError as exc:
            st.error(f'Report could not be completed. {exc}')
    saved = st.session_state.get(f'{prefix}:generated')
    if saved and saved[0] == scope:
        with st.expander('Report contents'):
            st.dataframe(saved[2], hide_index=True)
        data, extension, mime = saved[1]
        if extension == 'zip':
            st.info('Share report.md together with all CSV files in the archive.')
        st.download_button('Download report',data,f'okoo-marketing-{start}-{end}.{extension}',mime)
