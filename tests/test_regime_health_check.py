"""Offline unit tests for regime_health_check.py and workflow failure/monthly contracts."""
from datetime import date, datetime
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import regime_health_check as rhc


def _make_vol_series(n_days=600, r_first=0.008, r_last=0.008, split_last=252, end='2026-11-20', trend_up=True):
    """Build deterministic alternating-return price series with exact target 1Y/2Y annualized volatility."""
    idx = pd.bdate_range(end=end, periods=n_days)
    prices = np.empty(n_days, dtype=float)
    prices[0] = 100.0
    drift = 0.0005 if trend_up else -0.0002
    for i in range(1, n_days):
        in_last = i >= (n_days - split_last)
        amp = r_last if in_last else r_first
        sign = 1.0 if (i % 2 == 0) else -1.0
        prices[i] = prices[i - 1] * (1.0 + drift + sign * amp)
    return pd.Series(prices, index=idx, name='Close')


def _make_whipsaw_series(n_warmup=260, cycles=10, down_trend=True, end='2026-11-20'):
    """Build a deterministic series oscillating cleanly across its 220MA to trigger grace=0 breaks."""
    vals = [100.0] * n_warmup
    step = -0.35 if down_trend else 3.0
    amp = 18.0 if down_trend else 25.0
    for c_idx in range(cycles):
        anchor = 100.0 + c_idx * step
        vals.extend([anchor + amp] * 16)
        vals.extend([anchor - amp] * 16)
    if not down_trend:
        # Finish above rising anchor so 2Y return is strongly positive while preserving all 16 cycles
        vals.extend([vals[-32] + amp + 10.0] * 10)
    idx = pd.bdate_range(end=end, periods=len(vals))
    return pd.Series(vals, index=idx, name='Close')


def _make_steady_bull(n_days=1600, daily_ret=0.0007, start_val=100.0, end='2026-11-20'):
    idx = pd.bdate_range(end=end, periods=n_days)
    vals = start_val * ((1.0 + daily_ret) ** np.arange(n_days))
    return pd.Series(vals, index=idx, name='Close')


class FakeTelegramResponse:
    def __init__(self, body=b'{"ok":true,"result":{"message_id":99}}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class AlarmTests(unittest.TestCase):
    def test_alarm4_volatility_green_yellow_red_and_recovery(self):
        # Low vol & strong uptrend -> green + recovery_ready=True
        low_s = _make_vol_series(r_first=0.006, r_last=0.006, trend_up=True)  # ~9.5% vol
        res_low = rhc.evaluate_alarm4_volatility(low_s, 'QQQ')
        self.assertEqual(res_low['status'], 'green')
        self.assertTrue(res_low['above_220ma'])
        self.assertTrue(res_low['recovery_ready'])

        # 1Y ~31.9% (0.0201 * sqrt(252)) & 2Y ~28.4% (first half 0.0155 * sqrt(252)) -> yellow, not red
        yel_s = _make_vol_series(n_days=550, r_first=0.0155, r_last=0.0201, split_last=252, trend_up=False)
        res_yel = rhc.evaluate_alarm4_volatility(yel_s, '006208')
        self.assertGreaterEqual(res_yel['vol_1y_pct'], 30.0)
        self.assertLess(res_yel['vol_2y_pct'], 30.0)
        self.assertEqual(res_yel['status'], 'yellow')
        self.assertFalse(res_yel['recovery_ready'])

        # 1Y >= 35% -> red immediately on 1Y extreme
        red_1y = _make_vol_series(n_days=550, r_first=0.010, r_last=0.024, split_last=252, trend_up=False)
        res_red1 = rhc.evaluate_alarm4_volatility(red_1y, '006208')
        self.assertGreaterEqual(res_red1['vol_1y_pct'], 35.0)
        self.assertEqual(res_red1['status'], 'red')

        # 1Y ~31.5% AND 2Y ~31.5% -> sustained >30% -> red
        red_2y = _make_vol_series(n_days=550, r_first=0.020, r_last=0.020, split_last=252, trend_up=False)
        res_red2 = rhc.evaluate_alarm4_volatility(red_2y, 'QQQ')
        self.assertGreaterEqual(res_red2['vol_1y_pct'], 30.0)
        self.assertGreaterEqual(res_red2['vol_2y_pct'], 30.0)
        self.assertEqual(res_red2['status'], 'red')

        # Low vol (<25%) but BELOW 220MA -> recovery_ready must remain False
        low_below = _make_vol_series(n_days=550, r_first=0.005, r_last=0.005, trend_up=True)
        low_below.iloc[-1] = low_below.rolling(220).mean().iloc[-1] * 0.90
        res_not_rec = rhc.evaluate_alarm4_volatility(low_below, 'QQQ')
        self.assertLess(res_not_rec['vol_1y_pct'], 25.0)
        self.assertFalse(res_not_rec['above_220ma'])
        self.assertFalse(res_not_rec['recovery_ready'])

    def test_eod_incomplete_bar_drop(self):
        idx = pd.to_datetime(['2026-11-20', '2026-11-23'])
        tw_s = pd.Series([100.0, 101.0], index=idx)
        before_close_tw = datetime(2026, 11, 23, 10, 30, tzinfo=ZoneInfo('Asia/Taipei'))
        after_close_tw = datetime(2026, 11, 23, 13, 35, tzinfo=ZoneInfo('Asia/Taipei'))
        self.assertEqual(str(rhc._drop_incomplete_eod_bar(tw_s, '006208.TW', before_close_tw).index[-1].date()), '2026-11-20')
        self.assertEqual(str(rhc._drop_incomplete_eod_bar(tw_s, '006208.TW', after_close_tw).index[-1].date()), '2026-11-23')
        self.assertEqual(str(rhc._drop_incomplete_eod_bar(tw_s, 'IR0001', before_close_tw).index[-1].date()), '2026-11-20')
        self.assertEqual(str(rhc._drop_incomplete_eod_bar(tw_s, 'IR0001', after_close_tw).index[-1].date()), '2026-11-23')

        us_before = datetime(2026, 11, 23, 14, 0, tzinfo=ZoneInfo('America/New_York'))
        us_after = datetime(2026, 11, 23, 16, 1, tzinfo=ZoneInfo('America/New_York'))
        self.assertEqual(str(rhc._drop_incomplete_eod_bar(tw_s, 'QQQ', us_before).index[-1].date()), '2026-11-20')
        self.assertEqual(str(rhc._drop_incomplete_eod_bar(tw_s, 'QQQ', us_after).index[-1].date()), '2026-11-23')

    def test_alarm2_whipsaw_requires_negative_2y_return(self):
        bull = _make_steady_bull(n_days=600)
        self.assertEqual(rhc.evaluate_alarm2_whipsaw(bull, 8, 220, 6, 'TW')['status'], 'green')

        # Many breaks (> 6 in 504d) AND negative 2Y return -> red
        chop_down = _make_whipsaw_series(n_warmup=260, cycles=16, down_trend=True)
        # Ensure the trailing bar is below 505-bars-ago reference while remaining strictly positive
        chop_down.iloc[-1] = min(chop_down.iloc[-1], chop_down.iloc[-505] * 0.85)
        res_red = rhc.evaluate_alarm2_whipsaw(chop_down, 8, 220, 6, 'TW')
        self.assertGreater(res_red['breaks_2y'], 6)
        self.assertLess(res_red['ret_2y_pct'], 0.0)
        self.assertEqual(res_red['status'], 'red')

        # Exact same break structure (> 6) but positive 2Y return -> MUST NOT be red
        chop_up = _make_whipsaw_series(n_warmup=260, cycles=16, down_trend=False)
        res_safe = rhc.evaluate_alarm2_whipsaw(chop_up, 8, 220, 6, 'TW')
        self.assertGreater(res_safe['breaks_2y'], 6)
        self.assertGreater(res_safe['ret_2y_pct'], 0.0)
        self.assertNotEqual(res_safe['status'], 'red')

    def test_alarm3_us_rates_requires_weak_qqq(self):
        idx = pd.bdate_range(end='2026-11-20', periods=400)
        qqq_strong = _make_steady_bull(n_days=400, daily_ret=0.0008)
        qqq_weak = pd.Series(100.0 * (0.9992 ** np.arange(400)), index=idx)

        irx_low = pd.Series(4.1, index=idx)
        self.assertEqual(rhc.evaluate_alarm3_us_rates(irx_low, qqq_weak)['status'], 'green')

        irx_yel = pd.Series(5.6, index=idx)
        self.assertEqual(rhc.evaluate_alarm3_us_rates(irx_yel, qqq_weak)['status'], 'yellow')

        # High rate >= 95% days > 6% AND weak QQQ -> red
        irx_high = pd.Series(6.25, index=idx)
        res_red = rhc.evaluate_alarm3_us_rates(irx_high, qqq_weak)
        self.assertTrue(res_red['sustained_gt6'])
        self.assertTrue(res_red['us_weak'])
        self.assertEqual(res_red['status'], 'red')

        # High rate > 6% BUT strong QQQ -> NOT red
        res_yel = rhc.evaluate_alarm3_us_rates(irx_high, qqq_strong)
        self.assertTrue(res_yel['sustained_gt6'])
        self.assertFalse(res_yel['us_weak'])
        self.assertEqual(res_yel['status'], 'yellow')


class Layer4RedlineTests(unittest.TestCase):
    def test_10y_steady_bull_all_green(self):
        us = _make_steady_bull(n_days=2500, daily_ret=0.0008)
        tw = _make_steady_bull(n_days=2500, daily_ret=0.0007)
        irx = pd.Series(2.0, index=us.index)
        res = rhc.evaluate_layer4_redlines(us, tw, irx)
        self.assertEqual(res['status'], 'green')
        self.assertEqual(res['redline_a']['status'], 'green')
        self.assertEqual(res['redline_b']['status'], 'green')
        self.assertEqual(res['redline_c']['status'], 'green')
        self.assertGreater(res['redline_a']['diff_3y_pct'], 0.0)

    def test_redline_a_3y_lag_over_12pct(self):
        # Slow 8-day climbs into breakouts at 3.0x followed by sudden 2-day -2.7% air-pockets:
        # 1x finishes ~flat positive while leveraged bleeds deeply -> 3Y lag << -12%/yr
        n_warm = 500
        n_tail = 850
        idx = pd.bdate_range(end='2026-11-20', periods=n_warm + n_tail)
        warm = list(100.0 * (1.0006 ** np.arange(n_warm)))
        p = warm[-1]
        tail_vals = []
        for i in range(n_tail):
            p = p * (1.007 if (i % 10) < 8 else 0.973)
            tail_vals.append(p)
        grinding = pd.Series(warm + tail_vals, index=idx)
        res = rhc.evaluate_layer4_redlines(grinding, grinding.copy(), pd.Series(6.5, index=idx))
        self.assertIsNotNone(res['redline_a']['diff_3y_pct'])
        self.assertLess(res['redline_a']['diff_3y_pct'], -12.0)
        self.assertEqual(res['redline_a']['status'], 'red')

    def test_redline_b_underwater_over_880_days(self):
        idx = pd.bdate_range(end='2026-11-20', periods=1600)
        up = 100.0 * (1.0020 ** np.arange(600))
        # Drop 25% and drift sideways below peak for 1000 trading days
        flat = np.full(1000, up[-1] * 0.75)
        s = pd.Series(np.concatenate([up, flat]), index=idx)
        irx = pd.Series(1.0, index=idx)
        res = rhc.evaluate_layer4_redlines(s, s.copy(), irx)
        self.assertGreater(res['redline_b']['current_uw_days'], 880)
        self.assertEqual(res['redline_b']['status'], 'red')

    def test_redline_c_active_drawdown_vs_recovered_historical_crash(self):
        # 1) Active deep crash (-55% / -70%+) -> red + tsunami flag
        idx = pd.bdate_range(end='2026-11-20', periods=700)
        # Gentle ramp (0.04%/day) reaches 3.0x without US Extension Guard cap, then 2-day sudden gap down
        up = 100.0 * (1.0004 ** np.arange(690))
        crash_tail = up[-1] * np.array([0.82, 0.72] + [0.72] * 8)
        s_crash = pd.Series(np.concatenate([up, crash_tail]), index=idx)
        irx = pd.Series(1.0, index=idx)
        res_crash = rhc.evaluate_layer4_redlines(s_crash, s_crash.copy(), irx)
        self.assertLessEqual(res_crash['redline_c']['active_dd_pct'], -50.0)
        self.assertEqual(res_crash['redline_c']['status'], 'red')

        # 2) Extreme crash <= -70% -> red + tsunami_breached=True
        idx_tsu = pd.bdate_range(end='2026-11-20', periods=700)
        up_t = 100.0 * (1.0004 ** np.arange(690))
        tsu_tail = up_t[-1] * np.array([0.78, 0.58] + [0.58] * 8)
        s_tsu = pd.Series(np.concatenate([up_t, tsu_tail]), index=idx_tsu)
        res_tsu = rhc.evaluate_layer4_redlines(s_tsu, s_tsu.copy(), pd.Series(1.0, index=idx_tsu))
        self.assertLessEqual(res_tsu['redline_c']['active_dd_pct'], -70.0)
        self.assertTrue(res_tsu['redline_c']['tsunami_breached'])
        self.assertEqual(res_tsu['redline_c']['status'], 'red')

        # 3) Historical -55% crash 4 years ago that fully recovered to new ATH -> redline_c must be GREEN now!
        idx_rec = pd.bdate_range(end='2026-11-20', periods=1600)
        p1 = 100.0 * (1.0004 ** np.arange(600))
        p2 = p1[-1] * np.array([0.75] * 10)  # temporary >50% leveraged drop years ago
        p3 = p2[-1] * (1.0012 ** np.arange(1, 991))  # climbs way above p1 ATH
        s_rec = pd.Series(np.concatenate([p1, p2, p3]), index=idx_rec)
        res_rec = rhc.evaluate_layer4_redlines(s_rec, s_rec.copy(), pd.Series(1.0, index=idx_rec))
        self.assertLessEqual(res_rec['redline_c']['max_dd_pct'], -50.0)
        self.assertEqual(res_rec['redline_c']['status'], 'green')


class TsmcAndNotificationTests(unittest.TestCase):
    def test_tsmc_evaluation_and_fallback_chain(self):
        self.assertEqual(rhc.evaluate_tsmc_weight({'weight_pct': 56.42, 'source': '006208.TW'})['status'], 'green')
        self.assertEqual(rhc.evaluate_tsmc_weight({'weight_pct': 32.0, 'source': '006208.TW'})['status'], 'yellow')
        self.assertEqual(rhc.evaluate_tsmc_weight({'weight_pct': 29.5, 'source': '006208.TW'})['status'], 'red')
        self.assertEqual(rhc.evaluate_tsmc_weight({'weight_pct': None, 'source': None})['status'], 'unknown')

        # Mock yfinance inside fetch_tsmc_weight: 006208 fails -> 0050 proxy succeeds
        proxy_df = pd.DataFrame({'Holding Percent': [0.5512]}, index=['2330.TW'])

        class FakeTicker:
            def __init__(self, sym):
                self.sym = sym

            @property
            def funds_data(self):
                if self.sym == '006208.TW':
                    raise RuntimeError('006208 blocked')
                return type('FD', (), {'top_holdings': proxy_df})()

        with mock.patch('yfinance.Ticker', side_effect=FakeTicker):
            info = rhc.fetch_tsmc_weight()
        self.assertEqual(info, {'weight_pct': 55.12, 'source': '0050.TW_proxy', 'error': None})

        # Both fail -> graceful None without exception
        with mock.patch('yfinance.Ticker', side_effect=RuntimeError('offline')):
            degraded = rhc.fetch_tsmc_weight()
        self.assertIsNone(degraded['weight_pct'])
        self.assertIsNotNone(degraded['error'])

    def test_notification_gating_and_telegram_formatting(self):
        base_report = {
            'as_of_date': '2026-11-01',
            'data_dates': {'us': '2026-10-30', 'tw': '2026-10-30', 'irx': '2026-10-30'},
            'overall_status': 'green',
            'has_red': False,
            'has_yellow': False,
            'red_items': [],
            'yellow_items': [],
            'alarm4': {
                'us': {'status': 'green', 'vol_1y_pct': 18.0, 'vol_2y_pct': 19.0, 'above_220ma': True, 'recovery_ready': True},
                'tw': {'status': 'green', 'vol_1y_pct': 20.0, 'vol_2y_pct': 21.0, 'above_220ma': True, 'recovery_ready': True},
            },
            'alarm2': {
                'us': {'status': 'green', 'breaks_2y': 2, 'limit_2y': 10, 'ret_2y_pct': 30.0},
                'tw': {'status': 'green', 'breaks_2y': 1, 'limit_2y': 6, 'ret_2y_pct': 45.0},
            },
            'alarm3': {'status': 'green', 'latest_pct': 4.1, 'avg_1y_pct': 3.9, 'pct_days_gt6': 0.0, 'us_weak': False},
            'layer4': {
                'status': 'green', 'sim_start': '2017-10-18',
                'redline_a': {'status': 'green', 'diff_3y_pct': 18.5},
                'redline_b': {'status': 'green', 'current_uw_days': 10, 'max_uw_days': 320, 'limit_days': 880},
                'redline_c': {'status': 'green', 'current_dd_pct': -3.2, 'worst_1m_dd_pct': -5.1, 'tsunami_breached': False},
            },
            'tsmc': {'status': 'green', 'weight_pct': 56.42, 'source': '006208.TW'},
        }

        # Non-Jan/Jul green -> silent
        self.assertEqual(rhc.should_notify(base_report, 'auto', date(2026, 11, 1)), (False, 'silent_sentinel_ok'))

        # Non-Jan/Jul yellow only -> silent (no spam)
        yel_report = json.loads(json.dumps(base_report))
        yel_report['overall_status'] = 'yellow'
        yel_report['has_yellow'] = True
        yel_report['yellow_items'] = ['鬧鐘④ 台股高波']
        self.assertEqual(rhc.should_notify(yel_report, 'auto', date(2026, 11, 1)), (False, 'silent_sentinel_ok'))

        # Non-Jan/Jul red -> immediate alarm
        red_report = json.loads(json.dumps(base_report))
        red_report['overall_status'] = 'red'
        red_report['has_red'] = True
        red_report['red_items'] = ['鬧鐘④ 台股高波']
        self.assertEqual(rhc.should_notify(red_report, 'auto', date(2026, 11, 1)), (True, 'red_alarm'))
        red_html = rhc.format_health_report_html(red_report, 'red_alarm')
        self.assertIn('體制鬧鐘觸發', red_html)
        self.assertIn('請將本報表貼給 AI', red_html)

        # January 1 green -> mandatory annual report + #rebal-panel + manual checklist
        jan_report = json.loads(json.dumps(base_report))
        jan_report['as_of_date'] = '2027-01-01'
        self.assertEqual(rhc.should_notify(jan_report, 'auto', date(2027, 1, 1)), (True, 'annual_report'))
        jan_html = rhc.format_health_report_html(jan_report, 'annual_report')
        self.assertIn('年度策略體檢＆半年再平衡', jan_html)
        self.assertIn('#rebal-panel', jan_html)
        self.assertIn('4,000 萬', jan_html)

        # July 1 green -> mandatory semiannual report + #rebal-panel
        jul_report = json.loads(json.dumps(base_report))
        jul_report['as_of_date'] = '2027-07-01'
        self.assertEqual(rhc.should_notify(jul_report, 'auto', date(2027, 7, 1)), (True, 'semiannual_report'))
        jul_html = rhc.format_health_report_html(jul_report, 'semiannual_report')
        self.assertIn('年中體制快報＆半年再平衡', jul_html)
        self.assertIn('#rebal-panel', jul_html)

        # dry-run never notifies even with red
        self.assertEqual(rhc.should_notify(red_report, 'dry-run', date(2027, 1, 1)), (False, 'dry_run'))
        self.assertEqual(rhc.should_notify(base_report, 'force-notify', date(2026, 11, 23)), (True, 'manual_force'))

    def test_run_health_check_dry_run_makes_no_telegram_call_and_writes_json(self):
        us = _make_steady_bull(n_days=1600, daily_ret=0.0007)
        tw = _make_steady_bull(n_days=1600, daily_ret=0.0006)
        irx = pd.Series(4.0, index=us.index)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_json = Path(tmpdir) / 'report.json'
            with mock.patch.object(rhc, 'fetch_close_series', side_effect=[us, tw, irx]), \
                 mock.patch.object(rhc, 'fetch_tsmc_weight', return_value={'weight_pct': 55.0, 'source': '006208.TW', 'error': None}), \
                 mock.patch.object(rhc, 'send_telegram_html') as send_mock, \
                 mock.patch.object(sys, 'stdout', new=io.StringIO()):
                report = rhc.run_health_check(mode='dry-run', as_of_str='2026-11-23', json_out=str(out_json))
            send_mock.assert_not_called()
            saved = json.loads(out_json.read_text(encoding='utf-8'))
            self.assertEqual(saved['overall_status'], report['overall_status'])
            self.assertEqual(saved['notify'], {'should_send': False, 'reason': 'dry_run'})

    def test_fetch_close_series_routes_ir0001_to_official_source(self):
        import tw_ir0001_source
        fake_s = _make_steady_bull(n_days=270, daily_ret=0.0005)
        fake_df = pd.DataFrame({'Close': fake_s})
        with mock.patch.object(tw_ir0001_source, 'fetch_tw_ir0001_data', return_value=fake_df) as m_ir:
            got = rhc.fetch_close_series('IR0001', period='10y', min_rows=260)
        m_ir.assert_called_once()
        self.assertGreaterEqual(len(got), 260)

    def test_ci_failure_alert_escaping_retries_and_missing_secret(self):
        html = rhc.format_ci_failure_html({
            'GITHUB_WORKFLOW': 'Deploy <Main> & "Pages"',
            'GITHUB_EVENT_NAME': 'schedule',
            'GITHUB_REPOSITORY': 'brian85812/Investment',
            'GITHUB_RUN_ID': '12345',
            'GITHUB_SERVER_URL': 'https://github.com',
            'GITHUB_SHA': 'abcdef123456',
        })
        self.assertIn('Deploy &lt;Main&gt; &amp; &quot;Pages&quot;', html)
        self.assertIn('https://github.com/brian85812/Investment/actions/runs/12345', html)

        # Missing secret -> safe skip (exit 0)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, 'stdout', new=io.StringIO()):
            rc = rhc.main(['--notify-ci-failure'])
        self.assertEqual(rc, 0)

        # Urgent delivery sends disable_notification=False
        captured = []

        def fake_urlopen(req, context=None, timeout=15):
            captured.append(json.loads(req.data.decode('utf-8')))
            return FakeTelegramResponse()

        with mock.patch.dict(os.environ, {'TG_BOT_TOKEN': 'tok', 'TG_CHAT_ID': 'cid'}, clear=True), \
             mock.patch.object(rhc.urllib.request, 'urlopen', side_effect=fake_urlopen), \
             mock.patch.object(sys, 'stdout', new=io.StringIO()):
            rc_ok = rhc.main(['--notify-ci-failure'])
        self.assertEqual(rc_ok, 0)
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0]['disable_notification'])

        # Retries 3 times on failure and returns exit code 1
        with mock.patch.dict(os.environ, {'TG_BOT_TOKEN': 'tok', 'TG_CHAT_ID': 'cid'}, clear=True), \
             mock.patch.object(rhc.urllib.request, 'urlopen', side_effect=OSError('net down')) as u_mock, \
             mock.patch.object(sys, 'stdout', new=io.StringIO()):
            rc_fail = rhc.main(['--notify-ci-failure'])
        self.assertEqual(rc_fail, 1)
        self.assertEqual(u_mock.call_count, 3)

        # Verify stdlib-only import chain for --notify-ci-failure in an isolated subprocess
        import subprocess
        probe = (
            f"import sys; sys.path.insert(0, {str(REPO)!r}); "
            "import regime_health_check as r; "
            "assert 'numpy' not in sys.modules and 'pandas' not in sys.modules and 'yfinance' not in sys.modules; "
            "raise SystemExit(r.main(['--notify-ci-failure']))"
        )
        sub_env = {k: v for k, v in os.environ.items() if k not in ('TG_BOT_TOKEN', 'TG_CHAT_ID')}
        proc = subprocess.run([sys.executable, '-c', probe], env=sub_env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class WorkflowContractTests(unittest.TestCase):
    def test_monthly_and_daily_workflows_have_health_check_and_failure_alert(self):
        monthly_path = REPO / '.github/workflows/regime_health_check.yml'
        daily_path = REPO / '.github/workflows/update_dashboard.yml'
        self.assertTrue(monthly_path.exists())
        monthly_txt = monthly_path.read_text(encoding='utf-8')
        self.assertIn("- cron: '0 9 1 * *'", monthly_txt)
        self.assertIn("timezone: 'Asia/Taipei'", monthly_txt)
        self.assertIn('workflow_dispatch:', monthly_txt)
        self.assertIn('regime_health_check.py --mode', monthly_txt)
        self.assertIn('if: failure()', monthly_txt)
        self.assertIn('--notify-ci-failure', monthly_txt)

        daily_txt = daily_path.read_text(encoding='utf-8')
        self.assertIn('Monthly Regime Health Check', daily_txt)
        self.assertIn('if: failure()', daily_txt)
        self.assertIn('regime_health_check.py --notify-ci-failure', daily_txt)
        self.assertIn('\n  build:\n', daily_txt)
        self.assertIn('\n  deploy:\n    needs: build\n', daily_txt)
        self.assertIn("steps.deployment.outcome == 'failure'", daily_txt)


if __name__ == '__main__':
    unittest.main()
