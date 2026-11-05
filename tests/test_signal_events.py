"""用真 compute_signal AST 與固定價格驗事件分類；禁止模組啟動或行情抓取。"""
import ast
import json
from pathlib import Path
import unittest

import numpy as np
import pandas as pd


APP_PATH = Path(__file__).resolve().parents[1] / 'app.py'
TW_PARAMS = dict(name='台股', ticker='006208.TW', base_leverage=1.0, max_leverage=3.0,
                 fast_ma=8, slow_ma=220, breakout_window=15, breakout_up=15, breakout_dn=25,
                 cooldown=10, allocs=[0., .50, .75, .90, 1.], grace_days=1,
                 bull_acceleration=dict(enable=True, accel_base=1.2, accel_days=252, min_bear_days=40),
                 deep_bear_guard=dict(bear_ma=60, ratio=.97, deep_base=.5),
                 add_above_bear=False, pre_bull_boost=1.2)


def price_fixture(tail=()):
    values = [100.] * 260 + list(np.arange(101., 122.)) + list(tail)
    idx = pd.bdate_range('2025-01-02', periods=len(values))
    return pd.DataFrame({col: values for col in ('Open', 'High', 'Low', 'Close')}, index=idx).assign(Volume=1000.)


def compute_from_source(frame, source_path=APP_PATH):
    tree = ast.parse(Path(source_path).read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'compute_signal')
    ns = dict(np=np, pd=pd, fetch_robust_market_data=lambda _: frame.copy(deep=True))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source_path), 'exec'), ns)
    return ns['compute_signal'](**TW_PARAMS)


class SignalEventTests(unittest.TestCase):
    def test_target_change_is_not_excess_trim(self):
        result = compute_from_source(price_fixture())
        event = result['last_signal_change']
        self.assertEqual((result['target_yesterday'], result['target_today']), (2.5, 2.8))
        self.assertTrue(result['signal_changed_today'])
        self.assertFalse(result['excess_trim_today'])
        self.assertIsNone(result['last_excess_trim'])
        self.assertEqual(event['kind'], 'signal_change')
        self.assertEqual(event['date'], result['date'])
        self.assertEqual(event['days_ago'], 0)
        self.assertTrue(event['event_id'].endswith('|2.50|2.80'))
        self.assertEqual(result['last_action'], {k: v for k, v in event.items() if k not in ('kind', 'event_id')})

    def test_unchanged_target_only_records_excess_trim(self):
        result = compute_from_source(price_fixture([116.]))
        self.assertEqual((result['target_yesterday'], result['target_today']), (2.8, 2.8))
        self.assertFalse(result['signal_changed_today'])
        self.assertTrue(result['excess_trim_today'])
        signal, trim = result['last_signal_change'], result['last_excess_trim']
        self.assertNotEqual(signal['date'], trim['date'])
        self.assertEqual(signal['days_ago'], 1)
        self.assertEqual(trim['date'], result['date'])
        self.assertEqual(trim['days_ago'], 0)
        self.assertEqual(trim['kind'], 'excess_trim')
        self.assertIn('+0.15x', trim['type'])
        self.assertEqual(result['last_action'], result['last_trim'])
        self.assertEqual(result['last_trim'], {k: v for k, v in trim.items() if k not in ('kind', 'event_id')})

    def test_following_close_does_not_create_new_event(self):
        before = compute_from_source(price_fixture([116.]))
        after = compute_from_source(price_fixture([116., 116.]))
        self.assertEqual(after['target_today'], 2.8)
        self.assertFalse(after['signal_changed_today'])
        self.assertFalse(after['excess_trim_today'])
        for key in ('last_signal_change', 'last_excess_trim'):
            self.assertEqual(before[key]['event_id'], after[key]['event_id'])
            self.assertEqual(before[key]['date'], after[key]['date'])
            self.assertEqual(before[key]['days_ago'] + 1, after[key]['days_ago'])
        json.dumps(after)  # 新欄位必須保留可序列化的API型別。

    def test_target_reduction_does_not_replace_excess_trim_history(self):
        result = compute_from_source(price_fixture([116., 116., 99., 99.]))
        signal, trim = result['last_signal_change'], result['last_excess_trim']
        self.assertTrue(result['signal_changed_today'])
        self.assertFalse(result['excess_trim_today'])
        self.assertEqual(signal['date'], result['date'])
        self.assertGreater(signal['from_lev'], signal['to_lev'])
        self.assertNotEqual(signal['date'], trim['date'])
        self.assertNotIn('+0.15x', signal['type'])
        self.assertIn('+0.15x', trim['type'])


if __name__ == '__main__':
    unittest.main()
