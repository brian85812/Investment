"""Offline notification regressions; every HTTP request is intercepted."""
from pathlib import Path
from unittest import mock
import copy
import importlib.util
import json
import os
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('notification_build_under_test', REPO / 'build_static.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def event(kind, date, before, after, age=0):
    return {
        'date': date, 'from_lev': before, 'to_lev': after, 'days_ago': age, 'idx': 10,
        'kind': kind, 'type': '+0.15x超標修剪' if kind == 'excess_trim' else '跌破通道減碼',
    }


def market(ticker, date='2026-11-04'):
    target = .8 if ticker == 'QQQ' else 2.8
    return {
        'ticker': ticker, 'id': ticker, 'name': ticker, 'date': date, 'close': 100.,
        'target_today': target, 'target_yesterday': target, 'trim_trigger_lev': target + .15,
        'sma_fast_val': 99., 'sma_slow_val': 90., 'sma_bear_val': 95.,
        'high_bw': 101., 'low_bw': 89., 'step_idx': 1, 'max_steps': 3, 'in_trend': True,
        'last_action': None, 'last_trim': None,
        'last_signal_change': None, 'last_excess_trim': None,
        'is_stale': False, 'data_status': 'ok',
    }


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok":true,"result":{"message_id":123}}'


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='notification_test_', dir=os.environ.get('STOCK_TEST_TMPDIR'))
        self.root = Path(self.temp.name)
        (self.root / 'static').mkdir()
        (self.root / 'templates').mkdir()
        (self.root / 'templates/index.html').write_text('<html></html>', encoding='utf-8')
        self.signals = [market('QQQ'), market('006208.TW')]
        self.rollover = {'tw': {'days_left': 20}, 'us': {'days_left': 20}}
        self.path = self.root / 'dist/.last_notified_event.json'
        self.path.parent.mkdir()
        self.payloads = []
        self.fail_transport = False
        self.patches = [
            mock.patch.dict(os.environ, {'TG_BOT_TOKEN': 'AUDIT_DUMMY', 'TG_CHAT_ID': 'AUDIT_DUMMY'}, clear=True),
            mock.patch.object(build, '__file__', str(self.root / 'build_static.py')),
            mock.patch.object(build.urllib.request, 'urlopen', side_effect=self.capture),
            mock.patch.object(build, 'compute_signal', side_effect=self.compute_signal),
            mock.patch.object(build, 'compute_rollover_radar', return_value=self.rollover),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def capture(self, request, timeout):
        if self.fail_transport:
            raise OSError('offline simulated failure')
        self.payloads.append(json.loads(request.data))
        return FakeResponse()

    def compute_signal(self, **kwargs):
        return copy.deepcopy(next(s for s in self.signals if s['ticker'] == kwargs['ticker']))

    def send(self):
        return build._notify_snapshot(self.signals, self.rollover, str(self.path))

    def tw_signal(self, date='2026-11-04'):
        tw = self.signals[1]
        tw['date'] = date
        tw['target_yesterday'] = 3.
        tw['last_signal_change'] = event('signal_change', date, 3., 2.8)
        return tw

    def test_other_market_date_does_not_repeat_tw_signal_alert(self):
        self.tw_signal()
        self.send()
        self.assertFalse(self.payloads[-1]['disable_notification'])
        self.signals[0]['date'] = '2026-11-05'
        self.send()
        self.assertTrue(self.payloads[-1]['disable_notification'])
        self.assertIn('本次維持，無新增調倉', self.payloads[-1]['text'])
        self.assertNotIn('（本次新訊號）', self.payloads[-1]['text'])
        self.assertEqual(len(self.payloads), 2)  # A new daily report remains permitted.

    def test_derived_age_and_index_changes_do_not_resend(self):
        tw = self.signals[1]
        tw['last_excess_trim'] = event('excess_trim', tw['date'], 3.04, 2.8)
        self.send()
        tw['last_excess_trim'].update(days_ago=1, idx=999)
        result = self.send()
        self.assertTrue(result['dedup_skipped'])
        self.assertEqual(len(self.payloads), 1)

    def test_same_level_new_event_date_is_not_suppressed(self):
        self.tw_signal('2026-11-04')
        self.send()
        self.tw_signal('2026-11-05')
        self.send()
        self.assertFalse(self.payloads[-1]['disable_notification'])
        self.assertIn('（本次新訊號）', self.payloads[-1]['text'])
        state = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(len(state['markets']['006208.TW']['signal_change']), 2)

    def test_fresh_excess_trim_with_unchanged_target_alerts(self):
        self.send()
        self.signals[1]['last_excess_trim'] = event('excess_trim', '2026-11-04', 3.04, 2.8)
        self.send()
        self.assertFalse(self.payloads[-1]['disable_notification'])
        self.assertIn('模擬超標修剪提醒', self.payloads[-1]['text'])
        self.assertNotIn('（本次新訊號）', self.payloads[-1]['text'])

    def test_historical_trim_and_defensive_state_are_not_new_signal(self):
        tw = self.signals[1]
        tw['deep_bear_active'] = True
        tw['last_excess_trim'] = event('excess_trim', '2026-11-03', 3.04, 2.8, 1)
        tw['last_action'] = tw['last_trim'] = tw['last_excess_trim']
        self.send()
        self.assertTrue(self.payloads[-1]['disable_notification'])
        self.assertIn('維持 <b>2.80x</b>', self.payloads[-1]['text'])
        self.assertNotIn('調倉指令警報', self.payloads[-1]['text'])

    def test_missing_configuration_does_not_consume_event(self):
        self.tw_signal()
        with mock.patch.dict(os.environ, {}, clear=True):
            result = self.send()
        self.assertEqual(result['status'], 'skipped_unconfigured')
        self.assertFalse(self.path.exists())
        self.assertTrue(self.send()['delivered'])
        self.assertEqual(len(self.payloads), 1)

    def test_failed_send_does_not_consume_event(self):
        self.tw_signal()
        self.fail_transport = True
        with self.assertRaises(RuntimeError):
            self.send()
        self.assertFalse(self.path.exists())
        self.fail_transport = False
        self.send()
        self.assertFalse(self.payloads[-1]['disable_notification'])

    def test_build_notify_uses_same_dedup_decision(self):
        self.tw_signal()
        build.build(notify=True)
        build.build(notify=True)
        self.assertEqual(len(self.payloads), 1)
        self.assertTrue(build.notify_from_dist()['dedup_skipped'])

    def test_build_only_does_not_reset_sent_events(self):
        self.tw_signal()
        build.build(notify=False)
        build.notify_from_dist()
        before = self.path.read_bytes()
        build.build(notify=False)
        self.assertTrue(build.notify_from_dist()['dedup_skipped'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_snapshot_preserves_identifiable_sent_signal(self):
        self.tw_signal()
        legacy = {'signals': [{key: s.get(key) for key in (
            'ticker', 'date', 'target_today', 'target_yesterday', 'is_stale', 'last_trim')}
            for s in self.signals], 'rollover': {'tw': 20, 'us': 20}}
        self.path.write_text(json.dumps({'last_event_sig': json.dumps(legacy)}), encoding='utf-8')
        self.signals[0]['date'] = '2026-11-05'
        self.send()
        self.assertTrue(self.payloads[-1]['disable_notification'])
        state = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(state['schema_version'], 2)
        self.assertEqual(len(state['markets']['006208.TW']['signal_change']), 1)

    def test_legacy_trim_migrates_by_original_event_date(self):
        tw = self.signals[1]
        tw['last_excess_trim'] = event('excess_trim', tw['date'], 3.04, 2.8)
        legacy = {'signals': [{
            'ticker': s['ticker'], 'date': s['date'], 'target_today': s['target_today'],
            'target_yesterday': s['target_yesterday'], 'last_trim': s['last_excess_trim'],
        } for s in self.signals], 'rollover': {'tw': 20, 'us': 20}}
        self.path.write_text(json.dumps({'last_event_sig': json.dumps(legacy)}), encoding='utf-8')
        tw['last_excess_trim']['days_ago'] = 5
        self.assertTrue(self.send()['dedup_skipped'])
        self.assertEqual(len(self.payloads), 0)

    def test_new_null_field_does_not_fall_back_to_mixed_history(self):
        tw = self.signals[1]
        tw['last_action'] = event('signal_change', tw['date'], 3., 2.8)
        tw['last_trim'] = event('excess_trim', tw['date'], 3.04, 2.8)
        self.send()
        self.assertTrue(self.payloads[-1]['disable_notification'])
        self.assertNotIn('（本次新訊號）', self.payloads[-1]['text'])
        self.assertNotIn('模擬超標修剪提醒', self.payloads[-1]['text'])
        self.assertNotIn('上次目標變更', self.payloads[-1]['text'])

    def test_old_api_classifies_trim_without_calling_it_target_change(self):
        tw = self.signals[1]
        del tw['last_signal_change'], tw['last_excess_trim']
        tw['last_action'] = tw['last_trim'] = event('excess_trim', tw['date'], 3.04, 2.8)
        self.send()
        self.assertIn('模擬超標修剪提醒', self.payloads[-1]['text'])
        self.assertNotIn('（本次新訊號）', self.payloads[-1]['text'])
        self.assertIn('實帳 &gt;2.95x 才降回 <b>2.80x</b>', self.payloads[-1]['text'])

    def test_old_api_floating_point_noise_is_not_a_new_signal(self):
        tw = self.signals[1]
        del tw['last_signal_change'], tw['last_excess_trim']
        tw['target_today'] = 2.8000000000000003
        tw['last_action'] = event('signal_change', tw['date'], 2.8, 2.8000000000000003)
        self.send()
        self.assertTrue(self.payloads[-1]['disable_notification'])
        self.assertNotIn('（本次新訊號）', self.payloads[-1]['text'])

    def test_unrecognized_legacy_state_does_not_pretend_sent(self):
        self.tw_signal()
        self.path.write_text('{"last_event_sig":"bad legacy value"}', encoding='utf-8')
        self.send()
        self.assertFalse(self.payloads[-1]['disable_notification'])

    def test_workflow_uses_attempt_specific_matching_cache_keys(self):
        workflow = (REPO / '.github/workflows/update_dashboard.yml').read_text(encoding='utf-8')
        keys = [line.strip().removeprefix('key: ') for line in workflow.splitlines()
                if line.strip().startswith('key: tg-notify-dedup-')]
        self.assertEqual(keys, ['tg-notify-dedup-${{ github.run_id }}-${{ github.run_attempt }}'] * 2)
        self.assertIn('            tg-notify-dedup-\n', workflow)  # Restore old cache keys too.
        self.assertEqual(len({keys[0].replace('${{ github.run_id }}', '200')
                              .replace('${{ github.run_attempt }}', attempt) for attempt in ('1', '2')}), 2)

    def test_stale_market_has_precise_warning_without_new_adjustment_or_sent_event(self):
        tw = self.tw_signal('2026-11-05')
        tw.update(expected_session='2026-11-06', is_stale=True, data_status='stale')
        tw['last_excess_trim'] = event('excess_trim', tw['date'], 3.04, 2.8)
        self.send()
        text = self.payloads[-1]['text']
        self.assertIn('台股資料延遲：11/05，應11/06', text)
        self.assertIn('暫停調倉指示', text)
        self.assertNotIn('（本次新訊號）', text)
        self.assertNotIn('今日觸發模擬修剪提醒', text)
        self.assertIn('台股 11/05', text)  # The affected market's data date stays explicit.
        self.assertNotIn('實帳 &gt;2.95x', text)  # No stale-market trim instruction.
        state = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertFalse(state['markets']['006208.TW'])

    def test_current_date_fallback_is_not_called_delay_or_consumed(self):
        tw = self.tw_signal()
        tw.update(expected_session=tw['date'], fallback_used=True, data_status='ok')
        self.send()
        text = self.payloads[-1]['text']
        self.assertIn('台股來源失敗，使用備援資料', text)
        self.assertNotIn('台股資料延遲', text)
        self.assertNotIn('（本次新訊號）', text)
        tw['fallback_used'] = False
        self.send()
        self.assertIn('（本次新訊號）', self.payloads[-1]['text'])

    def test_cached_fallback_stale_without_a_known_date_gap_does_not_invent_delay(self):
        tw = self.tw_signal()
        tw.update(expected_session=tw['date'], data_status='fallback_stale')
        self.send()
        self.assertIn('來源失敗，使用備援資料', self.payloads[-1]['text'])
        self.assertNotIn('台股資料延遲', self.payloads[-1]['text'])
        self.assertNotIn('（本次新訊號）', self.payloads[-1]['text'])

    def test_expected_date_alone_blocks_stale_direct_notification(self):
        tw = self.tw_signal()
        tw['expected_session'] = '2026-11-05'
        pending = {'006208.TW': build._market_events(tw)}
        build.send_push_notification(self.signals, self.rollover, pending_events=pending)
        self.assertIn('台股資料延遲：11/04，應11/05', self.payloads[-1]['text'])
        self.assertNotIn('（本次新訊號）', self.payloads[-1]['text'])

    def test_compact_cooldown_is_shown_once_without_channel_repetition(self):
        self.signals[1]['strategy_state'] = {
            'schema_version': 1, 'label': '多頭階梯',
            'cooldown': {'active': True, 'remaining_days': 9, 'applies_to_current_stage': True},
            'grace': {'active': True, 'days_elapsed': 1, 'days_allowed': 1},
            'd1': {'blocked': False}, 'blocked_actions': [],
        }
        self.send()
        text = self.payloads[-1]['text']
        self.assertEqual(text.count('冷卻餘 9 交易日'), 1)
        self.assertNotIn('年線容忍', text)  # Detailed state remains on the dashboard.
        self.assertNotIn('加碼 ', text)
        self.assertNotIn('上次目標變更', text)

    def test_no_new_action_is_short_and_keeps_real_account_condition(self):
        self.send()
        text = self.payloads[-1]['text']
        self.assertIn('本次維持，無新增調倉', text)
        self.assertIn('美股 11/04：維持 <b>0.80x</b>', text)
        self.assertIn('台股 11/04：維持 <b>2.80x</b>', text)
        self.assertIn('實帳超過目標+0.15x仍須核對修剪', text)
        self.assertLessEqual(len(text), 180)
        self.assertTrue(self.payloads[-1]['disable_notification'])

    def test_new_signal_and_excess_trim_are_both_kept_in_one_market_line(self):
        tw = self.tw_signal()
        tw['last_excess_trim'] = event('excess_trim', tw['date'], 3.04, 2.8)
        self.send()
        text = self.payloads[-1]['text']
        line = next(line for line in text.splitlines() if line.startswith('• 台股'))
        self.assertIn('3.00x → 2.80x', line)
        self.assertIn('模擬超標修剪提醒', line)
        self.assertIn('實帳 &gt;2.95x 才降回 <b>2.80x</b>', line)
        self.assertNotIn('無新增調倉', text)
        self.assertFalse(self.payloads[-1]['disable_notification'])

    def test_degraded_market_does_not_hide_other_markets_new_signal(self):
        tw = self.tw_signal()
        tw.update(expected_session='2026-11-05', is_stale=True)
        us = self.signals[0]
        us['target_yesterday'] = 2.23
        us['last_signal_change'] = event('signal_change', us['date'], 2.23, .8)
        self.send()
        text = self.payloads[-1]['text']
        self.assertIn('資料異常', text)
        self.assertIn('目標 <b>2.23x → 0.80x</b>（本次新訊號）', text)
        tw_line = next(line for line in text.splitlines() if line.startswith('• 台股'))
        self.assertIn('暫停調倉指示', tw_line)
        self.assertNotIn('（本次新訊號）', tw_line)
        self.assertNotIn('無新增調倉', text)

    def test_rollover_deadline_is_an_action_even_without_new_target(self):
        self.rollover['tw']['days_left'] = 0
        self.send()
        text = self.payloads[-1]['text']
        self.assertIn('需確認換約', text)
        self.assertIn('台指期：今日結算，立即確認換約', text)
        self.assertNotIn('本次維持，無新增調倉', text)
        self.assertFalse(self.payloads[-1]['disable_notification'])

    def test_rollover_watch_remains_quiet_and_uses_short_text(self):
        self.rollover['tw']['days_left'] = 5
        self.send()
        self.assertIn('台指期：結算倒數 5 天，請確認換約', self.payloads[-1]['text'])
        self.assertTrue(self.payloads[-1]['disable_notification'])

    def test_rerun_cache_keeps_new_delivery_for_the_next_run(self):
        cache = {}
        workflow = (REPO / '.github/workflows/update_dashboard.yml').read_text(encoding='utf-8')
        template = next(line.strip().removeprefix('key: ') for line in workflow.splitlines()
                        if line.strip().startswith('key: tg-notify-dedup-'))
        for run_id, attempt, date in (('200', '1', '2026-11-04'), ('200', '2', '2026-11-05'),
                                      ('201', '1', '2026-11-05')):
            key = template.replace('${{ github.run_id }}', run_id).replace('${{ github.run_attempt }}', attempt)
            if self.path.exists():
                self.path.unlink()
            restored = key if key in cache else (next(reversed(cache)) if cache else None)
            if restored:
                self.path.write_bytes(cache[restored])
            self.tw_signal(date)
            self.send()
            self.assertNotIn(key, cache)  # actions/cache keys are immutable.
            cache[key] = self.path.read_bytes()
        self.assertEqual(len(self.payloads), 2)  # The next run restores attempt 2, already sent.


if __name__ == '__main__':
    unittest.main()
