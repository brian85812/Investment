"""週末缺收盤列的回歸：官方原價只能經已驗證還原因子補入。"""
from datetime import datetime
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as dashboard


class SaturdayClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime.fromisoformat('2026-11-07T14:30:00+08:00')
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class Response:
    def __init__(self, value):
        self.value = value

    def read(self):
        return json.dumps(self.value).encode('utf-8')


def frame(adjustment=1.0):
    index = pd.bdate_range(end='2026-11-06', periods=300)
    data = pd.DataFrame({col: 222.55 * adjustment for col in ('Open', 'High', 'Low', 'Close')}, index=index)
    data['Volume'] = 1000.0
    data.loc['2026-11-06', 'Close'] = np.nan
    return data


def official_rows():
    return [
        ['115/11/05', '1000', '0', '222.55', '222.55', '222.55', '222.55', '0', '1'],
        ['115/11/06', '20972896', '0', '203.00', '217.70', '201.10', '210.10', '-12.45', '1'],
    ]


class MarketDataFreshnessTests(TestCase):
    def patch_rows(self, data, rows):
        def get(request, **kwargs):
            # 只為目標月提供成功資料；其他月份不可被當成已驗證的交易日全集。
            return Response({'stat': 'OK', 'data': rows if 'date=20261101' in request.full_url else []})
        with patch.object(dashboard, 'datetime', SaturdayClock), patch.object(dashboard.urllib.request, 'urlopen', side_effect=get):
            return dashboard.auto_patch_twse('006208.TW', data.copy(deep=True))

    def test_existing_nan_friday_close_is_repaired_on_saturday(self):
        result = self.patch_rows(frame(), official_rows())
        self.assertAlmostEqual(result.loc['2026-11-06', 'Close'], 210.10)
        self.assertAlmostEqual(result.loc['2026-11-06', 'Open'], 203.00)
        self.assertEqual(result.loc['2026-11-06', 'Volume'], 20972896)

    def test_repair_preserves_adjusted_price_basis(self):
        result = self.patch_rows(frame(adjustment=0.9), official_rows())
        self.assertAlmostEqual(result.loc['2026-11-06', 'Close'], 210.10 * 0.9)
        self.assertNotEqual(result.loc['2026-11-06', 'Close'], 210.10)

    def test_unverified_predecessor_keeps_gap_unfilled(self):
        data = frame().drop(pd.Timestamp('2026-11-05'))
        result = self.patch_rows(data, official_rows()[1:])
        self.assertTrue(pd.isna(result.loc['2026-11-06', 'Close']))
        self.assertTrue(result.attrs.get('unverified_twse_patch_skipped'))

    def test_one_missing_closed_session_tries_direct_source(self):
        stale = frame().iloc[:-1]
        fresh = frame()
        fresh.loc['2026-11-06', ['Open', 'High', 'Low', 'Close']] = [203.0, 217.7, 201.1, 210.1]
        with patch.object(dashboard, 'datetime', SaturdayClock), \
             patch.object(dashboard.yf, 'download', return_value=stale), \
             patch.object(dashboard, 'fetch_yahoo_direct', return_value=fresh) as direct, \
             patch.object(dashboard, 'auto_patch_twse', side_effect=lambda ticker, value: value), \
             patch.object(dashboard, 'apply_data_patches', side_effect=lambda ticker, value: value):
            result = dashboard.fetch_robust_market_data('006208.TW')
        direct.assert_called_once_with('006208.TW')
        self.assertEqual(result.index[-1], pd.Timestamp('2026-11-06'))
        self.assertFalse(result.attrs['is_stale'])


if __name__ == '__main__':
    main()
