import csv
import io
from unittest.mock import patch

from frontend.views import table


def test_explicit_csv_download_preserves_values_and_does_not_rerun():
    rows = [{'Campaign': 'Citizen, 日本', 'Spend': 'CAD 12.00', 'Conversions': 0, 'Unavailable': None}]
    with patch('frontend.views.st') as ui:
        table(rows, download_name='google-campaign-performance_2026-11-01_2026-11-14.csv')
    args = ui.download_button.call_args.kwargs
    assert args['on_click'] == 'ignore'
    assert args['mime'] == 'text/csv'
    assert args['data'].startswith(b'\xef\xbb\xbf')
    decoded = list(csv.DictReader(io.StringIO(args['data'].decode('utf-8-sig'))))
    assert decoded == [{'Campaign': 'Citizen, 日本', 'Spend': 'CAD 12.00', 'Conversions': '0', 'Unavailable': ''}]


def test_no_download_for_empty_table():
    with patch('frontend.views.st') as ui:
        table([], download_name='empty.csv')
    ui.download_button.assert_not_called()
