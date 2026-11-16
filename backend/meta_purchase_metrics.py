"""Aggregate the connector's canonical website purchase fields, never action aliases."""
from math import fsum

from backend.metrics import ratio

PURCHASE_NOTE = (
    'Meta website purchases/value sum reported offsite_conversion.fb_pixel_purchase values only. '
    'Missing daily fields remain unknown, not zero. Positive totals with missing rows are reported subtotals; '
    'CPA and ROAS then use those reported purchases/value and the selected-period spend. '
    'A zero is shown only when every selected daily row explicitly reports zero. '
    'Daily ad attribution settings are retained; mixed settings are not a single campaign attribution window.'
)


def purchase_totals(rows, spend=None, coverage=False):
    result = {}
    for key in ('website_purchases', 'website_purchase_value'):
        values = [row.get(key) for row in rows]
        reported = [value for value in values if value is not None]
        missing = len(values) - len(reported)
        value = fsum(reported) if reported else None
        # An explicit zero on one day cannot establish zero across missing days.
        result[key] = None if value == 0 and missing else value
        result[key + '_missing_rows'] = missing
        result[key + '_status'] = ('Unavailable' if result[key] is None else
                                  'Reported subtotal' if missing or not coverage else 'Reported total')
    result['website_cpa'] = ratio(spend, result['website_purchases']) if coverage else None
    result['website_roas'] = ratio(result['website_purchase_value'], spend) if coverage else None
    result['purchase_attribution_settings'] = sorted({row.get('attribution_setting') or 'Unknown' for row in rows})
    return result
