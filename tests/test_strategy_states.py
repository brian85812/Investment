"""真compute_signal AST的狀態觀測回歸；固定行情，不import啟動app或抓價。"""
import ast
import json
from pathlib import Path
import unittest
import numpy as np
import pandas as pd

APP_PATH = Path(__file__).resolve().parents[1] / 'app.py'
TW = dict(name='台股', ticker='IR0001', base_leverage=1., max_leverage=3.,
          fast_ma=8, slow_ma=220, breakout_window=15, breakout_up=15, breakout_dn=25,
          cooldown=10, allocs=[0., .5, .75, .9, 1.], grace_days=0,
          bull_acceleration=dict(enable=True, min_bear_days=40, accel_base=1.2, accel_days=252),
          deep_bear_guard=dict(bear_ma=60, ratio=.97, deep_base=.5),
          add_above_bear=False, pre_bull_boost=1.2)
US = dict(name='美股', ticker='QQQ', base_leverage=.8, max_leverage=3.,
          fast_ma=5, slow_ma=220, breakout_window=8, breakout_up=8, breakout_dn=8,
          cooldown=8, allocs=[0., .65, .85, 1.], grace_days=0,
          extension_guard=dict(threshold=1.16, cap=1.8),
          deep_bear_guard=dict(bear_ma=60, ratio=.97, deep_base=.4),
          add_above_bear=True, pre_bull_boost=1.)


def frame(values, **attrs):
    idx = pd.bdate_range(end='2026-11-06', periods=len(values))
    result = pd.DataFrame({c: values for c in ('Open', 'High', 'Low', 'Close')}, index=idx).assign(Volume=1000.)
    result.attrs.update(attrs)
    return result


def down_fixture(tail=(126.,) * 5):
    # 10/30為真正階梯降檔；其後五個市場bar，不用calendar day近似。
    return frame([100.] * 260 + list(np.arange(101., 142.)) + [141.] * 10 + [126.] + list(tail))


def fixtures():
    deep = [100.] * 260 + list(np.arange(99., 49., -1.))
    return {
        'tw_down_cd5': (down_fixture(), TW),
        'tw_blocked_down': (down_fixture([125., 124., 123., 122., 121.]), TW),
        'tw_sim_trim_cd': (down_fixture([120.] * 5), TW),
        'tw_exit_during_cd': (down_fixture([126.] * 3 + [80., 80.]), TW),
        'tw_grace': (frame([100.] * 260 + list(np.arange(101., 122.)) + [95.]), {**TW, 'grace_days': 1}),
        'tw_grace_counter2': (frame([100.] * 260 + list(np.arange(101., 122.)) + [95., 95.]), {**TW, 'grace_days': 3}),
        'tw_g0_single_day_exit': (frame([100.] * 260 + list(np.arange(101., 122.)) + [95.]), TW),
        'tw_deep': (frame(deep), TW),
        'tw_recovered': (frame(deep + [80.] * 8), TW),
        'tw_e3b': (frame(deep + [80.] * 8 + [81.]), TW),
        'tw_accel_last': (frame(deep + [110.] * 257), TW),
        'tw_accel_expired': (frame(deep + [110.] * 258), TW),
        'tw_full': (frame([100.] * 260 + list(np.arange(101., 132.))), TW),
        'tw_no_step': (frame([100.] * 260), TW),
        'us_extension': (frame([100.] * 260 + list(np.arange(101., 118.))), US),
        'us_d1': (frame([100.] * 260 + [140.] * 30 + [115.] * 10 + [116.]), US),
        'us_d1_cd': (frame([100.] * 260 + [140.] * 30 + [115.] * 4 + [116.]), US),
    }


def compute(prices, params, path=APP_PATH):
    fn = next(n for n in ast.parse(Path(path).read_text(encoding='utf-8')).body
              if isinstance(n, ast.FunctionDef) and n.name == 'compute_signal')
    env = dict(pd=pd, np=np, fetch_robust_market_data=lambda _: prices.copy(deep=True))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), 'exec'), env)
    return env['compute_signal'](**params)


class StrategyStateTests(unittest.TestCase):
    def result(self, key):
        prices, params = fixtures()[key]
        return compute(prices, params)

    def test_decrease_cooldown_uses_market_bars(self):
        r = self.result('tw_down_cd5')
        c = r['strategy_state']['cooldown']
        self.assertEqual((r['target_today'], c['last_step_event_kind']), (2.8, 'step_down'))
        self.assertEqual(c['last_step_event_date'], '2026-10-30')
        self.assertEqual((c['total_days'], c['elapsed_days'], c['remaining_days']), (10, 5, 5))
        self.assertTrue(c['active'])
        self.assertIn('減碼後階梯冷卻', r['strategy_state']['label'])
        self.assertEqual(r['cooldown_days_remaining'], 5)

    def test_broken_down_channel_is_really_blocked_by_cooldown(self):
        r = self.result('tw_blocked_down')
        state = r['strategy_state']
        self.assertTrue(state['triggers']['breakout_down'])
        self.assertEqual(r['target_today'], 2.8)
        self.assertEqual({b['action'] for b in state['blocked_actions'] if b['reason'] == 'cooldown'},
                         {'step_up', 'step_down'})
        self.assertTrue(any(b['action'] == 'step_down' and b['reason'] == 'cooldown' and b['trigger_met']
                            for b in state['blocked_actions']))

    def test_simulated_trim_does_not_restart_step_cooldown(self):
        r = self.result('tw_sim_trim_cd')
        self.assertIsNotNone(r['last_excess_trim'])
        self.assertNotEqual(r['last_excess_trim']['date'], '2026-10-30')
        self.assertEqual(r['strategy_state']['cooldown']['last_step_event_date'], '2026-10-30')
        self.assertEqual(r['cooldown_days_remaining'], 5)

    def test_annual_defense_bypasses_running_step_timer(self):
        r = self.result('tw_exit_during_cd')
        s = r['strategy_state']
        self.assertFalse(r['in_trend'])
        self.assertEqual(r['target_today'], 1.)
        self.assertTrue(s['cooldown']['timer_active'])
        self.assertFalse(s['cooldown']['active'])
        self.assertTrue(s['priority_defense']['bypasses_step_cooldown'])
        self.assertIn('annual_grace_exhausted', s['priority_defense']['reasons'])

    def test_grace_exposes_actual_counter(self):
        g = self.result('tw_grace')['strategy_state']['grace']
        self.assertTrue(g['active'])
        self.assertEqual((g['days_elapsed'], g['days_allowed'], g['exit_after_consecutive_days']), (1, 1, 2))
        r = self.result('tw_grace_counter2')
        self.assertEqual(r['strategy_state']['grace']['days_elapsed'], 2)
        self.assertIn('第2/3', r['explanation'])

    def test_tw_production_grace_zero_exits_on_first_day_below_annual(self):
        r = self.result('tw_g0_single_day_exit')
        s = r['strategy_state']
        self.assertFalse(r['in_trend'])
        self.assertEqual(r['target_today'], 1.0)
        self.assertFalse(s['grace']['active'])
        self.assertEqual((s['grace']['days_allowed'], s['grace']['exit_after_consecutive_days']), (0, 1))
        self.assertIn('annual_grace_exhausted', s['priority_defense']['reasons'])

        root = Path(__file__).resolve().parents[1]
        for rel_path in ('app.py', 'build_static.py'):
            tree = ast.parse((root / rel_path).read_text(encoding='utf-8'))
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'compute_signal'
                and any(kw.arg == 'ticker' and ast.literal_eval(kw.value) == 'IR0001' for kw in node.keywords)
            ]
            self.assertEqual(len(calls), 1, rel_path)
            grace_kw = next(kw for kw in calls[0].keywords if kw.arg == 'grace_days')
            self.assertEqual(ast.literal_eval(grace_kw.value), 0, rel_path)

        eng_tree = ast.parse((root / 'scratch' / 'engine.py').read_text(encoding='utf-8'))
        tw_assign = next(
            n for n in eng_tree.body
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'TW_P' for t in n.targets)
        )
        eng_grace = next(kw for kw in tw_assign.value.keywords if kw.arg == 'grace_days')
        self.assertEqual(ast.literal_eval(eng_grace.value), 0)

    def test_deep_recovery_and_e3b_have_distinct_stages(self):
        for key, stage, target in [('tw_deep', 'defense_deep', .5),
                                   ('tw_recovered', 'defense_recovered', 1.),
                                   ('tw_e3b', 'e3b', 1.2)]:
            with self.subTest(key=key):
                r = self.result(key)
                self.assertEqual((r['strategy_state']['stage'], r['target_today']), (stage, target))
                self.assertTrue(r['strategy_state']['priority_defense']['bypasses_step_cooldown'])

    def test_last_acceleration_day_is_still_active(self):
        r = self.result('tw_accel_last')
        self.assertEqual(r['target_today'], 1.2)
        self.assertTrue(r['bull_accel_active'])
        a = r['strategy_state']['bull_acceleration']
        self.assertEqual((a['days_including_data_bar'], a['days_remaining_after_data_bar']), (1, 0))
        after = self.result('tw_accel_expired')
        self.assertFalse(after['bull_accel_active'])
        self.assertEqual(after['target_today'], 1.)

    def test_d1_extension_and_cooldown_can_coexist(self):
        d1 = self.result('us_d1')['strategy_state']
        self.assertTrue(d1['d1']['blocked'])
        self.assertFalse(d1['cooldown']['active'])
        simultaneous = self.result('us_d1_cd')['strategy_state']
        self.assertTrue(simultaneous['d1']['blocked'])
        self.assertTrue(simultaneous['cooldown']['active'])
        self.assertIn('d1_filter', simultaneous['flags'])
        self.assertIn('cooldown', simultaneous['flags'])
        ext = self.result('us_extension')['strategy_state']
        self.assertTrue(ext['extension']['capping_active'])
        self.assertTrue(ext['cooldown']['active'])
        self.assertFalse(any(b['reason'] == 'extension' for b in ext['blocked_actions']))

    def test_full_step_and_just_started_timer_are_separate(self):
        r = self.result('tw_full')
        s = r['strategy_state']
        self.assertEqual(s['stage'], 'full')
        self.assertIn('full', s['flags'])
        self.assertTrue(s['cooldown']['step_event_on_data_bar'])
        self.assertTrue(s['cooldown']['active'])
        self.assertFalse(s['cooldown']['blocked_on_data_bar'])
        self.assertEqual(s['cooldown']['remaining_days'], 10)

    def test_no_fabricated_previous_step_and_state_is_json_serializable(self):
        c = self.result('tw_no_step')['strategy_state']['cooldown']
        self.assertIsNone(c['last_step_event_date'])
        self.assertIsNone(c['elapsed_days'])
        self.assertFalse(c['active'])
        for prices, params in fixtures().values():
            json.dumps(compute(prices, params), ensure_ascii=False)

    def test_stale_explanation_is_asof_and_never_an_order(self):
        prices = down_fixture()
        prices.attrs.update(is_stale=True, stale_days=1, expected_session='2026-11-09')
        r = compute(prices, TW)
        self.assertTrue(r['is_stale'])
        self.assertIn('截至2026-11-06資料日狀態', r['explanation'])
        self.assertIn('尚未更新', r['explanation'])
        for phrase in ['今日請', '今日無須', '今日完全不動作', '請掛限價']:
            self.assertNotIn(phrase, r['explanation'])

    def test_v_rebound_triggers_exits_and_hands_off_seamlessly(self):
        tw_v = {**TW, 'v_rebound_boost': {'win': 20, 'thresh': 0.15, 'lev': 2.0}}
        us_v = {**US, 'v_rebound_boost': {'win': 20, 'thresh': 0.15, 'lev': 2.0}}

        # 1) C1 deep bear overrides V-rebound even after a +16% bounce from low
        r_deep = compute(frame([200.] * 260 + [150.] * 35 + [175.]), us_v)
        self.assertTrue(r_deep['deep_bear_active'])
        self.assertFalse(r_deep['v_rebound_active'])
        self.assertEqual(r_deep['target_today'], 0.4)
        self.assertEqual(r_deep['strategy_state']['stage'], 'defense_deep')

        # 2) Below 220MA (not deep bear): +15.6% bounce from 20d low activates 2.0x
        base_bounce = [200.] * 260 + [160.] * 8 + [185.]
        r_on = compute(frame(base_bounce), tw_v)
        self.assertFalse(r_on['in_trend'])
        self.assertFalse(r_on['deep_bear_active'])
        self.assertTrue(r_on['v_rebound_active'])
        self.assertEqual(r_on['target_today'], 2.0)
        self.assertEqual(r_on['strategy_state']['stage'], 'v_rebound')
        self.assertIn('v_rebound', r_on['strategy_state']['flags'])
        self.assertEqual(r_on['last_signal_change']['type'], 'V轉先鋒啟動')

        # 3) Falling below 25d low stops out V-rebound back to base defense
        r_stop = compute(frame(base_bounce + [159.]), tw_v)
        self.assertFalse(r_stop['in_trend'])
        self.assertFalse(r_stop['v_rebound_active'])
        self.assertEqual(r_stop['target_today'], 1.0)
        self.assertEqual(r_stop['last_signal_change']['type'], 'V轉先鋒止損')

        # 4) Reclaiming 220MA at step=0 keeps 2.0x floor (seamless handoff, zero whipsaw drop to 1.0x)
        reclaim_seq = base_bounce + [199.] * 9
        r_handoff = compute(frame(reclaim_seq), tw_v)
        self.assertTrue(r_handoff['in_trend'])
        self.assertEqual(r_handoff['step_idx'], 0)
        self.assertTrue(r_handoff['v_rebound_active'])
        self.assertEqual(r_handoff['target_today'], 2.0)
        self.assertEqual(r_handoff['strategy_state']['stage'], 'v_rebound_handoff')

        # 5) Stepping up >= 2.0x hands off cleanly to main step ladder
        r_handed = compute(frame(reclaim_seq + [201.]), tw_v)
        self.assertTrue(r_handed['in_trend'])
        self.assertEqual(r_handed['step_idx'], 1)
        self.assertFalse(r_handed['v_rebound_active'])
        self.assertEqual(r_handed['target_today'], 2.0)
        self.assertEqual(r_handed['strategy_state']['stage'], 'bull')

    def test_four_engine_v_rebound_parity(self):
        root = Path(__file__).resolve().parents[1]
        expected_dict = {'win': 20, 'thresh': 0.15, 'lev': 2.0}
        expected_tuple = (20, 0.15, 2.0)

        for rel_path in ('app.py', 'build_static.py'):
            tree = ast.parse((root / rel_path).read_text(encoding='utf-8'))
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'compute_signal'
                and any(kw.arg == 'ticker' and ast.literal_eval(kw.value) in ('QQQ', 'IR0001') for kw in node.keywords)
            ]
            self.assertEqual(len(calls), 2, rel_path)
            for call in calls:
                vr_kw = next(kw for kw in call.keywords if kw.arg == 'v_rebound_boost')
                self.assertEqual(ast.literal_eval(vr_kw.value), expected_dict, rel_path)

        for rel_path in ('scratch/engine.py', 'regime_health_check.py'):
            tree = ast.parse((root / rel_path).read_text(encoding='utf-8'))
            for var_name in ('US_P', 'TW_P'):
                assign = next(
                    n for n in tree.body
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == var_name for t in n.targets)
                )
                vr_kw = next(kw for kw in assign.value.keywords if kw.arg == 'v_rebound')
                self.assertEqual(ast.literal_eval(vr_kw.value), expected_tuple, f'{rel_path}:{var_name}')


if __name__ == '__main__':
    unittest.main()
