"""Offline unit tests for tw_ir0001_source.py (all HTTP calls mocked)."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tw_ir0001_source as src


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def roc_str(d):
    return f'{d.year - 1911}/{d.month:02d}/{d.day:02d}'


def make_finmind_history(end_date, count=240, base_price=80000.0):
    dates = pd.bdate_range(end=end_date, periods=count)
    return [
        {'date': ts.strftime('%Y-%m-%d'), 'stock_id': 'TAIEX', 'price': round(base_price + i * 50.0, 2)}
        for i, ts in enumerate(dates)
    ]


class TwIr0001SourceTests(unittest.TestCase):
    def test_finmind_mfi94u_and_mis_eod_pipeline_with_spot_ref(self):
        exp = date(2026, 11, 24)
        now_tw = datetime(2026, 11, 24, 13, 37, tzinfo=src.TZ_TAIPEI)
        fm_rows = make_finmind_history('2026-11-23', count=240, base_price=80000.0)
        fm_rows[-1]['price'] = 85000.0  # Intraday/stale number that MFI94U should overwrite

        def fake_urlopen(req, context=None, timeout=15):
            url = req.full_url
            if 'finmindtrade.com' in url:
                return FakeResponse({'status': 200, 'data': fm_rows})
            if 'MFI94U' in url:
                if 'date=20261101' in url:
                    return FakeResponse({'stat': 'OK', 'data': [['115/11/23', '85,129.92']]})
                return FakeResponse({'stat': 'OK', 'data': []})
            if 'mis.twse.com.tw' in url:
                return FakeResponse({'msgArray': [
                    {'c': 'IR0001', 'd': '20261124', 't': '13:30:00', 'z': '92785.74', 'y': '85129.92',
                     'o': '90534.59', 'h': '92919.96', 'l': '90534.59'},
                    {'c': 't00', 'd': '20261124', 't': '13:33:00', 'z': '40122.35', 'y': '36811.89'},
                ]})
            raise AssertionError(f'Unexpected URL: {url}')

        with mock.patch.object(src.urllib.request, 'urlopen', side_effect=fake_urlopen):
            df = src.fetch_tw_ir0001_data(
                expected_session=exp,
                is_tw_holiday_fn=lambda d: d.weekday() >= 5,
                now_tw=now_tw,
                sleep_fn=lambda _: None,
            )

        self.assertIsNotNone(df)
        self.assertEqual(df.index[-1].strftime('%Y-%m-%d'), '2026-11-24')
        self.assertAlmostEqual(float(df.loc['2026-11-23', 'Close']), 85129.92)
        self.assertAlmostEqual(float(df.loc['2026-11-24', 'Close']), 92785.74)
        self.assertAlmostEqual(float(df.loc['2026-11-24', 'Open']), 90534.59)
        self.assertIn('finmind', df.attrs['source_layer'])
        self.assertIn('twse_mfi94u', df.attrs['source_layer'])
        self.assertIn('twse_mis', df.attrs['source_layer'])
        self.assertEqual(df.attrs['spot_ref'], {
            'label': '加權', 'ticker': '^TWII', 'close': 40122.35, 'date': '2026-11-24'
        })

    def test_finmind_failure_falls_back_to_twse_mfi94u_history(self):
        exp = date(2026, 11, 24)
        now_tw = datetime(2026, 11, 24, 15, 0, tzinfo=src.TZ_TAIPEI)
        all_bdays = pd.bdate_range(end='2026-11-24', periods=250)

        def fake_urlopen(req, context=None, timeout=15):
            url = req.full_url
            if 'finmindtrade.com' in url:
                raise OSError('FinMind down')
            if 'MFI94U' in url:
                ym = url.split('date=')[1][:6]
                month_rows = [
                    [roc_str(ts.date()), f'{80000 + i * 10:,.2f}']
                    for i, ts in enumerate(all_bdays) if ts.strftime('%Y%m') == ym
                ]
                return FakeResponse({'stat': 'OK', 'data': month_rows})
            if 'mis.twse.com.tw' in url:
                return FakeResponse({'msgArray': []})
            raise AssertionError(f'Unexpected URL: {url}')

        with mock.patch.object(src.urllib.request, 'urlopen', side_effect=fake_urlopen):
            df = src.fetch_tw_ir0001_data(
                expected_session=exp,
                is_tw_holiday_fn=lambda d: d.weekday() >= 5,
                now_tw=now_tw,
                sleep_fn=lambda _: None,
                min_rows=225,
            )

        self.assertIsNotNone(df)
        self.assertEqual(len(df), 250)
        self.assertEqual(df.attrs['source_layer'], 'twse_mfi94u')

    def test_intraday_mis_bar_is_rejected_before_close(self):
        exp = date(2026, 11, 23)
        now_tw = datetime(2026, 11, 24, 11, 15, tzinfo=src.TZ_TAIPEI)
        fm_rows = make_finmind_history('2026-11-23', count=240, base_price=80000.0)

        def fake_urlopen(req, context=None, timeout=15):
            url = req.full_url
            if 'finmindtrade.com' in url:
                return FakeResponse({'status': 200, 'data': fm_rows})
            if 'MFI94U' in url:
                return FakeResponse({'stat': 'OK', 'data': []})
            if 'mis.twse.com.tw' in url:
                return FakeResponse({'msgArray': [
                    {'c': 'IR0001', 'd': '20261124', 't': '11:15:00', 'z': '91000.00', 'y': fm_rows[-1]['price']},
                    {'c': 't00', 'd': '20261124', 't': '11:15:00', 'z': '39500.00', 'y': '36811.89'},
                ]})
            raise AssertionError(f'Unexpected URL: {url}')

        with mock.patch.object(src.urllib.request, 'urlopen', side_effect=fake_urlopen):
            df = src.fetch_tw_ir0001_data(
                expected_session=exp,
                is_tw_holiday_fn=lambda d: d.weekday() >= 5,
                now_tw=now_tw,
                sleep_fn=lambda _: None,
            )

        self.assertEqual(df.index[-1].strftime('%Y-%m-%d'), '2026-11-23')
        self.assertIsNone(df.attrs.get('spot_ref'))

    def test_mis_prev_close_mismatch_refuses_blind_append(self):
        exp = date(2026, 11, 24)
        now_tw = datetime(2026, 11, 24, 13, 40, tzinfo=src.TZ_TAIPEI)
        fm_rows = make_finmind_history('2026-11-23', count=240, base_price=80000.0)

        def fake_urlopen(req, context=None, timeout=15):
            url = req.full_url
            if 'finmindtrade.com' in url:
                return FakeResponse({'status': 200, 'data': fm_rows})
            if 'MFI94U' in url:
                return FakeResponse({'stat': 'OK', 'data': []})
            if 'mis.twse.com.tw' in url:
                return FakeResponse({'msgArray': [
                    {'c': 'IR0001', 'd': '20261124', 't': '13:30:00', 'z': '92785.74', 'y': '70000.00'},
                ]})
            if 'MI_INDEX' in url:
                return FakeResponse({'tables': []})
            raise AssertionError(f'Unexpected URL: {url}')

        with mock.patch.object(src.urllib.request, 'urlopen', side_effect=fake_urlopen):
            df = src.fetch_tw_ir0001_data(
                expected_session=exp,
                is_tw_holiday_fn=lambda d: d.weekday() >= 5,
                now_tw=now_tw,
                sleep_fn=lambda _: None,
            )

        self.assertEqual(df.index[-1].strftime('%Y-%m-%d'), '2026-11-23')
        self.assertTrue(df.attrs.get('unverified_mis_patch_skipped'))

    def test_mi_index_fallback_fills_missing_expected_session(self):
        exp = date(2026, 11, 24)
        now_tw = datetime(2026, 11, 24, 14, 5, tzinfo=src.TZ_TAIPEI)
        fm_rows = make_finmind_history('2026-11-23', count=240, base_price=80000.0)

        def fake_urlopen(req, context=None, timeout=15):
            url = req.full_url
            if 'finmindtrade.com' in url:
                return FakeResponse({'status': 200, 'data': fm_rows})
            if 'MFI94U' in url:
                return FakeResponse({'stat': 'OK', 'data': []})
            if 'mis.twse.com.tw' in url:
                raise OSError('MIS timeout')
            if 'MI_INDEX' in url:
                return FakeResponse({'tables': [{
                    'title': '報酬指數',
                    'data': [
                        ['發行量加權股價指數', '40,122.35', '+', '3,310.46', '8.99'],
                        ['發行量加權股價報酬指數', '92,785.74', '+', '7,655.82', '8.99'],
                    ],
                }]})
            raise AssertionError(f'Unexpected URL: {url}')

        with mock.patch.object(src.urllib.request, 'urlopen', side_effect=fake_urlopen):
            df = src.fetch_tw_ir0001_data(
                expected_session=exp,
                is_tw_holiday_fn=lambda d: d.weekday() >= 5,
                now_tw=now_tw,
                sleep_fn=lambda _: None,
            )

        self.assertEqual(df.index[-1].strftime('%Y-%m-%d'), '2026-11-24')
        self.assertAlmostEqual(float(df.loc['2026-11-24', 'Close']), 92785.74)
        self.assertIn('twse_mi_index', df.attrs['source_layer'])
        self.assertEqual(df.attrs['spot_ref'], {
            'label': '加權', 'ticker': '^TWII', 'close': 40122.35, 'date': '2026-11-24'
        })


if __name__ == '__main__':
    unittest.main()
