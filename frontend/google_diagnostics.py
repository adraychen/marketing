from collections import defaultdict

from backend.metrics import summarize
from frontend.views import METRICS, number, period_rows

NOTE = (
    'Budget is the latest imported average daily budget, not a historical daily limit. '
    'Shared budgets belong to the whole budget pool; do not sum them across campaigns. '
    'Daily spend includes imported observations only. Lost IS columns show the minimum–maximum '
    'reported daily percentages, not period averages; missing days are excluded and coverage is shown. '
    'Google reports values above 90% as >90%. Search terms can be withheld by Google and do not '
    'reconcile to all campaign clicks. Conversions use configured Google Ads conversion actions; '
    'checkout starts are unavailable because conversion-action detail is not collected. '
    'Search terms are shown for factual review without automated relevance judgments.'
)


def share(value):
    return '—' if value is None else '>90%' if value == 0.9001 else f'{value:.2%}'


def daily_range(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return '—' if not values else share(min(values)) if min(values) == max(values) else f'{share(min(values))} – {share(max(values))}'


def tables(snapshot, start, end, currency):
    daily = defaultdict(list)
    for row in period_rows(snapshot, 'campaigns', start, end):
        daily[row['resource']].append(row)
    campaigns = {c['resource']: c for c in snapshot['campaigns']}
    groups = {g['resource']: g['parent'] for g in snapshot.get('ad_groups', [])}
    terms = defaultdict(list)
    term_rows = defaultdict(list)
    for row in period_rows(snapshot, 'search_terms', start, end):
        term_rows[row['resource']].append(row)
    for term in snapshot.get('search_terms', []):
        campaign = groups.get(term.get('parent'))
        if campaign in campaigns and term_rows[term['resource']]:
            metrics = summarize(term_rows[term['resource']], currency)
            terms[campaign].append({'Campaign': campaigns[campaign]['data']['name'],
                'Search term': term['data']['text'], 'Clicks': metrics['clicks'],
                'Spend': number(metrics['spend'], 'money', currency),
                'Google Ads conversions': metrics['conversions'], 'Checkout starts': None})
    summary, details = [], []
    for resource, item in campaigns.items():
        data, rows = item['data'], daily[resource]
        metrics = summarize(rows, currency) if rows else {}
        budget = data.get('daily_budget')
        summary.append({'Campaign': data['name'], 'Campaign ID': data.get('id', resource),
            'Current daily budget': number(budget, 'money', currency),
            'Shared budget': data.get('shared_budget'),
            'Budget resource': data.get('budget_resource'),
            'Daily spend (reported range)': '—' if not rows else
                f"{number(min(r['spend'] for r in rows), 'money', currency)} – {number(max(r['spend'] for r in rows), 'money', currency)}",
            **{f'Search lost IS ({reason}; daily range)': daily_range(rows, f'search_{reason}_lost_impression_share') for reason in ('budget', 'rank')},
            **{f'Lost IS {reason} days / period days': f"{sum(r.get(f'search_{reason}_lost_impression_share') is not None for r in rows)} / {(end-start).days+1}" for reason in ('budget', 'rank')},
            'Reported search terms': len(terms[resource]) if terms[resource] else None,
            'Search-term conversions (reported subtotal)': sum(t['Google Ads conversions'] for t in terms[resource]) if terms[resource] else None,
            **{label: number(metrics.get(key), kind, currency) for key, label, kind in METRICS}})
        for row in rows:
            details.append({'Campaign': data['name'], 'Date': row['day'],
                'Current daily budget': number(budget, 'money', currency),
                'Daily spend': number(row['spend'], 'money', currency),
                'Search lost IS (budget)': share(row.get('search_budget_lost_impression_share')),
                'Search lost IS (rank)': share(row.get('search_rank_lost_impression_share'))})
    return summary, details, [t for entries in terms.values() for t in entries]
