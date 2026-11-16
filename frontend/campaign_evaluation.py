"""Same campaign evidence in Streamlit and Markdown/CSV exports."""
import hashlib
import json

import streamlit as st

from frontend.client import APIError, call
from frontend.views import table


def load(base, start, end):
    return call(f'{base}/meta-ads/evaluation', params={'start': str(start), 'end': str(end)})


def analysis_key(base, start, end, campaign):
    fingerprint = hashlib.sha256(json.dumps(campaign, sort_keys=True, default=str).encode()).hexdigest()
    return f"campaign-ai:{base}:{start}:{end}:{fingerprint}"


def add_report(report, evidence, selected, base='', include_ai=False):
    report.note(evidence['policy'])
    report.table('Campaign evaluation source coverage', evidence['source_snapshots'])
    for campaign in evidence['campaigns']:
        if campaign['campaign_id'] not in selected:
            continue
        prefix = f"Campaign evaluation {campaign['campaign_id']}"
        report.note(f"{campaign['campaign']}: {campaign['confidence']} confidence. {campaign['confidence_basis']}")
        for label, key in (('funnel', 'funnel'), ('daily funnel', 'daily_funnel'), ('product destinations', 'destinations'),
                           ('product landing outcomes', 'product_outcomes'), ('GA4 campaign outcomes', 'ga4_campaign_outcomes'),
                           ('tracking diagnostics', 'diagnostics'), ('recommendations', 'recommendations')):
            report.table(f'{prefix} · {label}', campaign[key])
        report.table(f'{prefix} · profitability', [campaign['profitability']])
        report.table(f'{prefix} · data quality', [{'issue': issue} for issue in campaign['quality']])
        report.note(campaign['product_limitation'])
        if include_ai:
            saved = st.session_state.get(analysis_key(base, evidence['start'], evidence['end'], campaign))
            report.note(f"AI interpretation ({saved['model']}):\n\n{saved['summary']}" if saved else
                        'No saved AI interpretation matches this campaign, date range, and evidence. Deterministic recommendations are included above.')
    report.note('Unallocated traffic is retained below as storewide context, not credited to selected campaigns.')
    report.table('Unallocated GA4 traffic', evidence['unallocated_ga4_traffic'])
    report.note(f"Shopify UTM orders not uniquely allocated to a Meta campaign: {evidence['unallocated_shopify_orders']} (storewide context).")


def show(base, start, end, selected):
    try:
        evidence = load(base, start, end)
    except APIError as exc:
        st.info(str(exc))
        return
    st.caption(evidence['policy'])
    for campaign in evidence['campaigns']:
        if campaign['campaign_id'] not in selected:
            continue
        with st.expander(f"{campaign['campaign']} · {campaign['confidence']} confidence", expanded=True):
            st.caption(campaign['confidence_basis'])
            st.write('Meta advertising funnel')
            table([{'Stage': r['stage'], 'Count': r['count'], 'Availability': r['availability'],
                    'Rate from previous stage': r['preceding_stage_rate'], 'Cost per action': r['cost_per_action'],
                    'Previous period count': r['previous_count'], 'Change': r['change']} for r in campaign['funnel']])
            st.caption('Rates are decimal fractions. Later-stage rates are unavailable because these events are not a sequential cohort.')
            st.write('Product destinations and shopping outcomes')
            table(campaign['destinations'])
            table(campaign['product_outcomes'])
            table(campaign['ga4_campaign_outcomes'])
            st.caption(campaign['product_limitation'])
            st.write('Profitability by attribution source')
            table([campaign['profitability']])
            st.write('Recommendations from reported evidence')
            table(campaign['recommendations'])
            ai_key = analysis_key(base, start, end, campaign)
            st.caption('Optional AI sends aggregate metrics and diagnostic labels to Groq. Names, URLs, order IDs, and cost-model notes are excluded.')
            if st.button('Generate campaign AI analysis', key=f"generate-ai:{campaign['campaign_id']}"):
                try:
                    with st.spinner('Analyzing campaign evidence…'):
                        st.session_state[ai_key] = call(f"{base}/meta-ads/campaigns/{campaign['campaign_id']}/analysis", 'POST',
                                                       params={'start': str(start), 'end': str(end)}, timeout=60)
                except APIError as exc:
                    st.error(str(exc))
            if ai_key in st.session_state:
                st.write(st.session_state[ai_key]['summary'])
            with st.expander('Tracking diagnostics and data quality'):
                table(campaign['diagnostics'])
                for issue in campaign['quality']:
                    st.write('• ' + issue)
            with st.expander('Daily funnel changes'):
                table(campaign['daily_funnel'])
            with st.expander('Variable-cost assumptions'):
                st.caption('Saved by organization admins. These assumptions affect estimates only; no advertising settings change. '
                           'Product purchase costs come from Shopify variants. Explicit zero means no cost for that component.')
                model = campaign['profitability'].get('cost_model') or {}
                with st.form(f"cost-model:{campaign['campaign_id']}"):
                    currency = st.text_input('Cost currency', value=model.get('currency') or campaign['currency'] or 'CAD')
                    shipping = st.number_input('Shipping / fulfillment cost per order', min_value=0.0, value=float(model.get('shipping_per_order', 0)))
                    rate = st.number_input('Payment processing percentage', min_value=0.0, max_value=100.0, value=float(model.get('payment_rate', 0))*100)
                    fixed = st.number_input('Fixed payment fee per order', min_value=0.0, value=float(model.get('payment_per_order', 0)))
                    other = st.number_input('Other variable costs per order', min_value=0.0, value=float(model.get('other_per_order', 0)))
                    note = st.text_input('Assumption source / effective date', value=model.get('note', ''))
                    confirmed = st.checkbox('I verified these costs, including any zeros.')
                    if st.form_submit_button('Save cost assumptions'):
                        if not confirmed or not note.strip():
                            st.warning('Verify the assumptions and record their source before saving.')
                        else:
                            try:
                                call(f"{base}/meta-ads/campaigns/{campaign['campaign_id']}/cost-model", 'PUT',
                                     payload={'currency': currency, 'shipping_per_order': shipping, 'payment_rate': rate/100,
                                           'payment_per_order': fixed, 'other_per_order': other, 'note': note})
                                st.rerun()
                            except APIError as exc:
                                st.error(str(exc))
    with st.expander('Source coverage and unallocated traffic'):
        table(evidence['source_snapshots'])
        table(evidence['unallocated_ga4_traffic'])
        st.caption(f"Unallocated Shopify UTM orders: {evidence['unallocated_shopify_orders']}. Unknown sources are retained.")
