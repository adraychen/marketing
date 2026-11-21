"""Daily, per-ad charts from the existing Meta report snapshot."""
from datetime import timedelta

import plotly.graph_objects as go
import streamlit as st

from backend.connectors.meta_ads import action_value
from backend.metrics import ratio
from frontend.views import table

METRICS = {
    'Spend': ('spend', 'money'),
    'Impressions': ('impressions', 'count'),
    'Reach': ('reach', 'count'),
    'Frequency': ('frequency', 'multiple'),
    'Link clicks': ('inline_link_clicks', 'count'),
    'Link CTR': ('link_ctr', 'percent'),
    'Cost per link click': ('link_cpc', 'money'),
    'Landing-page views': ('landing_page_view', 'action'),
    'ViewContent': ('offsite_conversion.fb_pixel_view_content', 'action'),
    'Add to cart': ('offsite_conversion.fb_pixel_add_to_cart', 'action'),
    'Checkout starts': ('offsite_conversion.fb_pixel_initiate_checkout', 'action'),
    'Website purchases': ('website_purchases', 'count'),
    'Purchase value': ('website_purchase_value', 'money'),
    'Cost per purchase': ('website_cpa', 'money'),
    'ROAS': ('website_roas', 'multiple'),
}
COLORS = ('#007f78', '#5367c8', '#c07827', '#aa4b7b', '#4c879f', '#756544')


def ad_labels(data):
    campaigns = {r['id']: r.get('name') or r['id'] for r in data['campaigns']}
    labels = {}
    for row in [*data['ads'], *data['daily']]:
        identifier = row.get('ad_id') or row.get('id')
        name = row.get('name') or row.get('ad_name') or identifier
        labels.setdefault(identifier, f"{name} · {campaigns.get(row.get('campaign_id'), row.get('campaign_id', ''))} · {identifier}")
    return labels


def metric_value(row, metric):
    if row is None:
        return None
    field, kind = METRICS[metric]
    if kind == 'action':
        return action_value(row.get('actions'), field)
    if field in ('link_ctr', 'link_cpc', 'website_cpa', 'website_roas'):
        numerator, denominator = {
            'link_ctr': ('inline_link_clicks', 'impressions'),
            'link_cpc': ('spend', 'inline_link_clicks'),
            'website_cpa': ('spend', 'website_purchases'),
            'website_roas': ('website_purchase_value', 'spend'),
        }[field]
        return ratio(row.get(numerator), row.get(denominator))
    return row.get(field)


def daily_figure(data, selected, metric, start, end, currency):
    labels = ad_labels(data)
    dates = [start + timedelta(days=i) for i in range((end-start).days+1)]
    rows = {(r['ad_id'], str(r['day'])): r for r in data['daily']}
    kind = METRICS[metric][1]
    axis = f'{metric} ({currency})' if kind == 'money' else metric
    value_format = '.1%' if kind == 'percent' else ',.2f'
    fig = go.Figure()
    for identifier in selected:
        color = COLORS[sorted(labels).index(identifier) % len(COLORS)]
        values = [metric_value(rows.get((identifier, str(day))), metric) for day in dates]
        fig.add_trace(go.Scatter(x=dates, y=values, name=labels[identifier], mode='lines+markers',
                                 connectgaps=False, line={'color': color, 'width': 2}, marker={'size': 7},
                                 hovertemplate=f'%{{x|%b %d, %Y}}<br>{axis}: %{{y:{value_format}}}<extra>%{{fullData.name}}</extra>'))
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=25, b=10),
                      xaxis_title=None, yaxis_title=axis, hovermode='x unified',
                      legend=dict(orientation='h', y=-.25), template='plotly_white')
    fig.update_xaxes(type='date', range=[start, end+timedelta(days=1)])
    if kind == 'percent':
        fig.update_yaxes(tickformat='.1%')
    fig.update_yaxes(rangemode='tozero')
    return fig


def show_daily_charts(data, start, end, currency):
    st.subheader('Daily ad performance')
    labels = ad_labels(data)
    if not labels:
        st.info('No ads are available for the selected campaigns.')
        return
    selected = st.multiselect('Ads to chart', list(labels), default=list(labels)[:5],
                              format_func=labels.get, key='meta-daily-chart-ads:' + ':'.join(sorted(labels)))
    metrics = list(METRICS)
    account = data.get('account') or {}
    st.caption(f"Stored Meta data · Last sync: {account.get('synced_at', 'Unavailable')} · "
               f"Imported through: {account.get('synced_through', 'Unavailable')}. Each line represents one ad.")
    st.caption('Gaps mean unavailable data, not zero. Explicit zeros are plotted. Reach and frequency remain per ad per day. '
               'Purchases and revenue use Meta attribution; changing chart selections does not trigger a sync.')
    st.caption(f'Daily values, not cumulative · {start:%b %d, %Y} – {end:%b %d, %Y}. All metrics are shown below.')
    if not selected or not metrics:
        st.info('Select at least one ad to display charts.')
        return
    for offset in range(0, len(metrics), 2):
        for column, metric in zip(st.columns(2), metrics[offset:offset+2]):
            with column:
                st.write(metric)
                fig = daily_figure(data, selected, metric, start, end, currency)
                if any(value is not None for trace in fig.data for value in trace.y):
                    st.plotly_chart(fig, width='stretch', key=f'meta-daily-chart:{metric}')
                else:
                    st.info('No reported values for these ads and dates. Missing data is not plotted as zero.')
    with st.expander('View underlying daily records'):
        table([r for r in data['daily'] if r['ad_id'] in selected])
