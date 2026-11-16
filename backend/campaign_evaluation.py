"""Deterministic, exportable campaign evidence. Never combines platform conversions."""
from collections import defaultdict
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit, unquote
from backend.meta_purchase_metrics import PURCHASE_NOTE, purchase_totals

STAGES = (
    ('Impressions', 'impressions', None),
    ('Link clicks', 'inline_link_clicks', None),
    ('Landing-page views', None, 'landing_page_view'),
    ('Product views / ViewContent', None, 'offsite_conversion.fb_pixel_view_content'),
    ('Add to cart', None, 'offsite_conversion.fb_pixel_add_to_cart'),
    ('Checkout', None, 'offsite_conversion.fb_pixel_initiate_checkout'),
    ('Purchases', 'website_purchases', 'offsite_conversion.fb_pixel_purchase'),
)
GA_METRICS = ('sessions', 'itemViewEvents', 'addToCarts', 'checkouts', 'ecommercePurchases', 'purchaseRevenue')
NOTE = ('Meta actions are attributed event counts, not unique people or a sequential cohort. '
        'Only link clicks / impressions is shown as a comparable delivery rate. '
        'ViewContent does not prove a particular Shopify product was viewed. '
        'GA4 rows describe session-attributed outcomes, not purchased-item identity. '
        'Shopify uses eligible orders and last recorded visit UTMs, by order creation date. '
        'Never add purchases or revenue across these sources. ' + PURCHASE_NOTE)


def divide(n, d):
    return float(n) / float(d) if n is not None and d is not None and d > 0 else None


def total(rows, field=None, action=None):
    values = []
    for row in rows:
        value = row.get(field) if field else None
        if action and not field:
            matches = [x.get('value') for x in row.get('actions', []) if x.get('action_type') == action]
            value = matches[0] if len(matches) == 1 else None
        values.append(float(value) if value is not None else None)
    observed = [v for v in values if v is not None]
    return (sum(observed) if observed else None, len(observed) == len(rows) and bool(rows), len(observed))


def attribution(rows):
    return sorted({row.get('attribution_setting') or 'Unknown' for row in rows})


def funnel(current, previous, covered, prior_covered):
    spend, spend_complete, _ = total(current, 'spend')
    impressions, impressions_complete, _ = total(current, 'impressions')
    result = []
    for label, field, action in STAGES:
        count, complete, observed = total(current, field, action)
        old, old_complete, _ = total(previous, field, action)
        purchase_metrics = purchase_totals(current, spend if spend_complete else None, covered) if field == 'website_purchases' else None
        if purchase_metrics is not None:
            count = purchase_metrics['website_purchases']
            old = purchase_totals(previous)['website_purchases']
        comparable = (covered and prior_covered and complete and old_complete
                      and attribution(current) == attribution(previous) and len(attribution(current)) == 1
                      and 'Unknown' not in attribution(current))
        result.append({'stage': label, 'source': 'Meta', 'field': field or action,
                       'count': count, 'availability': 'Complete reported rows' if covered and complete else 'Partial reported count' if count is not None else 'Data unavailable',
                       'rows_with_metric': observed, 'daily_ad_rows': len(current),
                       'preceding_stage_rate': divide(count, impressions) if label == 'Link clicks' and covered and complete and impressions_complete else None,
                       'rate_definition': 'Link clicks / impressions; not a unique-user conversion rate' if label == 'Link clicks' else 'Not comparable as a sequential cohort',
                       'cost_per_action': purchase_metrics['website_cpa'] if purchase_metrics is not None else divide(spend, count) if covered and complete and spend_complete else None,
                       'previous_count': old, 'change': count-old if comparable else None,
                       'change_fraction': divide(count-old, old) if comparable else None})
    return result


def urls(creative):
    found = []
    if creative.get('link_url'):
        found.append(creative['link_url'])
    story = creative.get('object_story_spec') or {}
    for key in ('link_data', 'video_data'):
        block = story.get(key) or {}
        found.extend([block.get('link'), ((block.get('call_to_action') or {}).get('value') or {}).get('link')])
        found.extend(x.get('link') for x in block.get('child_attachments', []))
    found.extend(x.get('website_url') for x in (creative.get('asset_feed_spec') or {}).get('link_urls', []))
    return sorted({x for x in found if isinstance(x, str) and x})


def split_url(value):
    try:
        return urlsplit(value)
    except ValueError:
        return urlsplit('')


def product_handle(url):
    path = unquote(split_url(url).path).rstrip('/')
    parts = path.split('/')
    return parts[-1] if len(parts) >= 3 and parts[-2] == 'products' else None


def destinations(data, catalog, storefront):
    products = {v['product']['handle']: v['product'] for v in catalog}
    origin = split_url(storefront).hostname
    campaigns = {c['id']: c for c in data['campaigns']}
    adsets = {a['id']: a for a in data['adsets']}
    output = []
    for ad in data['ads']:
        creative = ad.get('creative') or {}
        for url in urls(creative) or ['']:
            issues = []
            values = parse_qs(split_url(url).query, keep_blank_values=True)
            tags = parse_qs(creative.get('url_tags') or '', keep_blank_values=True)
            for key, value in tags.items():
                if key in values and values[key] != value:
                    issues.append('Conflicting URL and creative UTM tags')
                values[key] = value
            replacements = {'campaign.id': ad.get('campaign_id', ''), 'campaign.name': campaigns.get(ad.get('campaign_id'), {}).get('name', ''),
                            'ad.id': ad['id'], 'ad.name': ad.get('name', ''), 'adset.id': ad.get('adset_id', ''),
                            'adset.name': adsets.get(ad.get('adset_id'), {}).get('name', '')}
            utm = {}
            for key in ('source', 'medium', 'campaign', 'content'):
                candidates = values.get('utm_' + key, [])
                value = candidates[0] if len(candidates) == 1 else ''
                for macro, replacement in replacements.items():
                    value = value.replace('{{' + macro + '}}', replacement or '')
                if '{{' in value:
                    issues.append('Unresolved dynamic UTM: ' + key)
                    value = ''
                utm[key] = value
            if not all(utm[k] for k in ('source', 'medium', 'campaign')):
                issues.append('Missing source, medium, or campaign UTM; campaign attribution unavailable')
            if any(utm[k] != utm[k].strip().lower() for k in ('source', 'medium')):
                issues.append('Source/medium case or whitespace differs from normalized naming; exact matching retained')
            parsed = split_url(url)
            handle = product_handle(url)
            product = products.get(handle) if parsed.hostname == origin and parsed.scheme == 'https' else None
            if not product:
                issues.append('Product destination unverified: needs a store product URL and imported Shopify catalog')
            if not utm['content']:
                issues.append('No ad-specific utm_content; individual-ad ecommerce attribution unavailable')
            output.append({'campaign_id': ad.get('campaign_id'), 'ad_id': ad['id'], 'destination': url,
                           'product_id': product['id'] if product else None, 'product': product.get('title') if product else None,
                           'handle': handle, 'utm': utm, 'issues': issues,
                           'matchable': bool(all(utm[k] for k in ('source', 'medium', 'campaign')) and not any('Conflicting' in i for i in issues))})
    return output


def key_for(row):
    return tuple(row.get(k, '') for k in ('source', 'medium', 'campaign'))


def registry(destinations):
    owners = defaultdict(set)
    for row in destinations:
        if row['matchable']:
            owners[key_for(row['utm'])].add(row['campaign_id'])
    return owners


def money(value):
    return Decimal(str(value))


def profit(orders, variants, spend, currency, model):
    """Current-cost estimate only. No inferred zero costs or cross-currency addition."""
    missing = []
    if spend is None or not model or model.get('currency') != currency:
        return {'estimated_contribution': None, 'break_even_acquisition_cost': None,
                'limitation': 'Needs complete spend and an explicit variable-cost model in the account currency.'}
    cogs, tax, revenue = Decimal(0), Decimal(0), Decimal(0)
    for order in orders:
        details = order.get('evaluation') or {}
        lines, taxes = details.get('line_items'), details.get('tax')
        if order['currency'] != currency or not lines or lines.get('pageInfo', {}).get('hasNextPage') or not taxes or taxes.get('currencyCode') != currency:
            missing.append('Order currency, tax, or complete line items unavailable')
            continue
        revenue += money(order['amount'])
        tax += money(taxes['amount'])
        for line in lines['nodes']:
            qty = line['currentQuantity']
            if qty == 0:
                continue
            variant = variants.get((order['shop'], (line.get('variant') or {}).get('id')), {})
            cost = (variant.get('inventoryItem') or {}).get('unitCost')
            if not cost or cost['currencyCode'] != currency:
                missing.append('Missing matching variant purchase cost or cost currency')
                continue
            cogs += money(cost['amount']) * qty
    if missing:
        return {'estimated_contribution': None, 'break_even_acquisition_cost': None, 'limitation': '; '.join(sorted(set(missing)))}
    variable = revenue * money(model['payment_rate']) + len(orders) * sum(money(model[k]) for k in ('shipping_per_order', 'payment_per_order', 'other_per_order'))
    before_ads = revenue - tax - cogs - variable
    return {'product_cost': float(cogs), 'tax_excluded': float(tax), 'modeled_variable_costs': float(variable),
            'estimated_contribution': float(before_ads - money(spend)),
            'break_even_acquisition_cost': float(before_ads / len(orders)) if orders else None,
            'cost_model': model,
            'limitation': 'Estimate uses current Shopify variant costs, not historical COGS. Excludes fixed overhead and unmodeled expenses.'}


def evaluate(data, previous, commerce, extra, status, start, end, storefront):
    snapshots = extra.get('snapshots', [])
    product_snaps = [s for s in snapshots if s['source'] == 'shopify_products']
    catalog = product_snaps[0]['data']['variants'] if len(product_snaps) == 1 else []
    variants = {(s['account'], v['id']): v for s in product_snaps for v in s['data']['variants']}
    links = destinations(data, catalog, storefront)
    owners = registry(links)
    ga_snaps = [s for s in snapshots if s['source'] == 'ga4_landing' and s['complete']]
    # Multiple properties are not assumed disjoint.
    ga_snap = ga_snaps[0] if len(ga_snaps) == 1 else None
    ga_rows = [r for r in ga_snap['data']['rows'] if str(start).replace('-', '') <= r['day'] <= str(end).replace('-', '')] if ga_snap else []
    account = data.get('account') or {}
    currency = account.get('data', {}).get('currency')
    zone = account.get('data', {}).get('timezone_name')
    order_coverage = [r for r in commerce.get('coverage', []) if r['source'] == 'shopify_orders' and r['complete']]
    order_ready = len(order_coverage) == 1
    current_runs = {r['source']: r for r in commerce.get('latest_runs', [])}
    results = []
    unmatched = [dict(source=r['source'], medium=r['medium'], campaign=r['campaign'], sessions=r['metrics'].get('sessions'))
                 for r in ga_rows if len(owners.get(key_for(r), set())) != 1]
    for campaign in data['campaigns']:
        cid = campaign['id']
        rows = [r for r in data['daily'] if r['campaign_id'] == cid]
        old = [r for r in previous['daily'] if r['campaign_id'] == cid]
        stages = funnel(rows, old, data['coverage'], previous['coverage'])
        spend, spend_complete, _ = total(rows, 'spend')
        spend_for_ratio = spend if data['coverage'] and spend_complete else None
        keys = {key for key, ids in owners.items() if ids == {cid}}
        mapped_orders = list({(o['shop'], o['order_id']): o for o in extra.get('orders', [])
                              if key_for(o['utm']) in keys and order_ready and o['shop'] == order_coverage[0]['account']}.values())
        my_links = [r for r in links if r['campaign_id'] == cid]
        issues = sorted({i for r in my_links for i in r['issues']})
        if len({(r['utm']['source'], r['utm']['medium']) for r in my_links if r['matchable']}) > 1:
            issues.append('Multiple source/medium names in this campaign; verify intentional placement naming. Exact values are retained.')
        if any(r['utm']['medium'] and r['utm']['medium'] not in {'cpc', 'ppc', 'paid_social', 'paid', 'social_paid'} for r in my_links):
            issues.append('Nonstandard paid medium in campaign links; check naming. These sources are retained, not automatically excluded.')
        if any(len(owners.get(key_for(r['utm']), set())) > 1 for r in my_links):
            issues.append('UTM tuple shared across campaigns; ambiguous outcomes are not allocated')
        if not keys:
            issues.append('No uniquely matched campaign UTM tuple; Shopify/GA4 campaign attribution unavailable')
        if any(s.get('stale') for s in snapshots) or data.get('stale'):
            issues.append('Some source snapshots are over 48 hours old')
        if not data['coverage']:
            issues.append('Meta date range not fully covered; totals are partial')
        if not previous['coverage']:
            issues.append('Previous period not covered; changes are unavailable')
        if len(attribution(rows)) != 1 or 'Unknown' in attribution(rows):
            issues.append('Mixed or unknown ad-level attribution settings; campaign aggregates are not a single attribution window')
        if order_ready and order_coverage[0]['timezone'] != zone:
            issues.append('Meta and Shopify reporting timezones differ; cross-source ratios withheld')
        if ga_snap and ga_snap['data']['timezone'] != zone:
            issues.append('Meta and GA4 reporting timezones differ')
        products = []
        grouped = {}
        for row in ga_rows:
            if key_for(row) not in keys:
                continue
            # Exact UTM tuple and product landing path, not a substring/name guess.
            # Optional hostname evidence must agree when supplied by a future importer.
            matching = [l for l in my_links if l['product_id'] and key_for(l['utm']) == key_for(row)
                        and (not row.get('host') or row['host'] == split_url(l['destination']).hostname)
                        and product_handle(row['landing_page']) == l['handle']]
            if not matching:
                continue
            product = matching[0]
            bucket = grouped.setdefault(product['product_id'], {'product_id': product['product_id'], 'product': product['product'],
                'source': 'GA4 product-landing sessions', 'currency': ga_snap['data']['currency'], **{k: 0 for k in GA_METRICS}})
            for metric in GA_METRICS:
                bucket[metric] += row['metrics'][metric]
        products.extend(grouped.values())
        ga_campaign = [r for r in commerce.get('ga4', []) if key_for(r) in keys]
        ga_coverage = [r for r in commerce.get('coverage', []) if r['source'] == 'ga4_store' and r['complete']]
        ga_ready = len(ga_coverage) == 1 and bool(keys)
        ga_purchases = sum(float(r['ga4_purchases']) for r in ga_campaign) if ga_ready else None
        shop_ready = order_ready and bool(keys) and not extra.get('warnings')
        shop_purchases = len(mapped_orders) if shop_ready else None
        shop_currency_ok = shop_ready and order_coverage[0]['currency'] == currency and all(o['currency'] == currency for o in mapped_orders)
        revenue = float(sum(money(o['amount']) for o in mapped_orders)) if shop_currency_ok else None
        aligned = shop_currency_ok and order_coverage[0]['timezone'] == zone
        model = next((m['data'] for m in extra.get('cost_models', []) if m['account_id'] == account.get('account_id') and m['campaign_id'] == cid), None)
        reported_purchases = purchase_totals(rows, spend_for_ratio, data['coverage'])
        purchase_count = reported_purchases['website_purchases']
        purchase_value = reported_purchases['website_purchase_value']
        purchases_complete = reported_purchases['website_purchases_status'] == 'Reported total'
        profitability = {'currency': currency, 'spend': spend,
            'cost_model': model,
            'meta_purchase_reporting': 'Complete reported rows' if purchases_complete and data['coverage'] else 'Partial or unavailable',
            'meta_attributed_purchases': purchase_count, 'meta_attributed_revenue': purchase_value,
            'meta_cost_per_purchase': reported_purchases['website_cpa'],
            'meta_roas': reported_purchases['website_roas'],
            'shopify_attributed_orders': shop_purchases, 'shopify_attributed_revenue': revenue,
            'shopify_cost_per_order': divide(spend_for_ratio, shop_purchases) if aligned else None,
            'shopify_roas': divide(revenue, spend_for_ratio) if aligned else None,
            **profit(mapped_orders, variants, spend_for_ratio if aligned else None, currency, model)}
        if ga_purchases is not None and shop_purchases is not None and ga_purchases != shop_purchases:
            issues.append(f'GA4 campaign purchases ({ga_purchases:g}) differ from Shopify UTM orders ({shop_purchases}); attribution/time bases differ, not an error by itself')
        ga_state = 'Data unavailable' if ga_purchases is None else 'No purchases recorded' if ga_purchases == 0 else 'Purchase events observed'
        meta_state = ('Data unavailable' if purchase_count is None else
                      'No purchases recorded' if purchase_count == 0 and purchases_complete and data['coverage'] else
                      'Partial purchase reporting; no positive count in available rows' if purchase_count == 0 else
                      'Purchase actions observed (possibly partial)')
        diagnostics = [
            {'source': 'Shopify', 'status': 'No purchases recorded' if shop_purchases == 0 else 'Completed orders observed' if shop_purchases else 'Data unavailable',
             'sync': current_runs.get('shopify_orders'), 'coverage': order_coverage,
             'attribution': commerce.get('attribution_model')},
            {'source': 'GA4', 'status': ga_state, 'verification': 'Purchase event not yet verified end-to-end',
             'sync': current_runs.get('ga4_store'), 'coverage': ga_coverage,
             'attribution': 'Session source / medium / campaign; no individual-ad dimension in imported report'},
            {'source': 'Meta', 'status': meta_state, 'verification': 'Purchase event not yet verified end-to-end',
             'last_success': account.get('synced_at'), 'attribution': attribution(rows),
             'pixel_dataset': [a.get('promoted_object') for a in data['adsets'] if a.get('campaign_id') == cid and a.get('promoted_object')]
                              or 'Configuration unavailable; event counts do not verify Pixel/CAPI deduplication'},
            {'source': 'Meta token', 'status': 'Expired' if status.get('token_expired') else 'Expiring soon' if status.get('token_expiring') else 'Expiry configured' if status.get('token_expires_at') else 'Unknown',
             'expiry': status.get('token_expires_at'), 'verification': status.get('expiry_source')},
        ]
        diagnostics.extend({'source': r['source'], 'status': r['status'], 'sync': r} for r in extra.get('runs', []))
        if not ga_snap:
            issues.append('GA4 product-landing coverage unavailable or multiple properties; run the detailed commerce sync')
        if not product_snaps:
            issues.append('Shopify product catalog/cost snapshot unavailable; verify read_products and optional read_inventory access')
        if len(product_snaps) > 1:
            issues.append('Multiple Shopify catalogs are present; product destination matching is withheld until store identity is disambiguated')
        issues.append('Product destinations and name-based UTM templates use latest ad metadata, not historical creative versions')
        for d in diagnostics:
            if isinstance(d.get('sync'), dict) and d['sync'].get('status') == 'failed':
                d['status'] = 'Tracking error: source sync failed; retained snapshot may be shown'
        if status.get('runs') and status['runs'][0]['status'] == 'failed':
            diagnostics[2]['status'] = 'Tracking error: Meta sync failed; retained snapshot shown'
        clicks = stages[1]['count']
        reliable = (data['coverage'] and not data.get('stale') and ga_ready and shop_ready
                    and all(l['matchable'] and owners.get(key_for(l['utm'])) == {cid} for l in my_links)
                    and not any(c.get('stale') for c in commerce.get('coverage', [])))
        confidence = 'Low' if not reliable or not clicks or clicks < 100 or shop_purchases is None or shop_purchases < 20 else 'Moderate'
        recommendations = []
        for index, stage in enumerate(stages[:4]):
            focus = ('Delivery', 'Engagement', 'Landing-page acquisition', 'Shopping behavior')[index]
            value = stage['count']
            recommendations.append({'area': focus, 'evidence': f"{stage['stage']}: {value if value is not None else 'unavailable'}; spend: {spend} {currency}",
                'recommendation': 'Monitor a longer comparable period before changing creative or budget.' if value is not None else 'Check source coverage and event reporting before judging performance.', 'confidence': confidence})
        recommendations[3]['evidence'] = '; '.join(f"Meta {s['stage']}: {s['count']}" for s in stages[3:6])
        recommendations.extend([
            {'area': 'Purchases', 'evidence': f'Meta: {purchase_count}; GA4: {ga_purchases}; Shopify UTM orders: {shop_purchases}',
             'recommendation': 'Keep source totals separate. Verify an existing purchase flow and transaction deduplication; do not install another tracker.', 'confidence': confidence},
            {'area': 'Profitability', 'evidence': f"Shopify ROAS: {profitability['shopify_roas']}; estimated contribution: {profitability['estimated_contribution']}",
             'recommendation': profitability['limitation'], 'confidence': confidence}])
        daily = []
        for day in sorted({r['day'] for r in rows}):
            daily.extend({'day': day, **s} for s in funnel([r for r in rows if r['day'] == day], [], data['coverage'], False))
        results.append({'campaign_id': cid, 'campaign': campaign.get('name'), 'currency': currency,
                        'confidence': confidence, 'confidence_basis': 'Heuristic: fewer than 100 link clicks or 20 Shopify-attributed orders, or missing coverage, is low confidence; not statistical significance.',
                        'funnel': stages, 'daily_funnel': daily, 'destinations': my_links,
                        'product_outcomes': products, 'ga4_campaign_outcomes': ga_campaign,
                        'profitability': profitability, 'diagnostics': diagnostics,
                        'quality': issues + extra.get('warnings', []), 'recommendations': recommendations,
                        'product_limitation': 'Matched by exact campaign UTMs and product landing path in the configured GA4 property. '
                        'Confirm that this property reports the intended storefront; the session landing dimension does not include hostname. '
                        'Outcomes belong to sessions landing on that path, not item sales. Ad-level outcomes require additional verified dimensions.'})
    return {'start': str(start), 'end': str(end), 'policy': NOTE, 'campaigns': results,
            'source_snapshots': [{**{k: v for k, v in s.items() if k != 'data'},
                                  **({k: s['data'].get(k) for k in ('cost_access', 'cost_note')} if s['source'] == 'shopify_products' else {})}
                                 for s in snapshots],
            'unallocated_ga4_traffic': unmatched,
            'unallocated_shopify_orders': sum(1 for o in extra.get('orders', []) if len(owners.get(key_for(o['utm']), set())) != 1)}
