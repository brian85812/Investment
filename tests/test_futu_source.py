"""富途接口與資料邊界回歸；所有行情均為合成 fixture，HTTP 全部攔截。"""
from datetime import date, datetime
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
import base64
import copy
import io
import json
import os
import sys
import urllib.error
import urllib.parse

import numpy as np
import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import futu_source as source
import app as dashboard

END = date(2026, 11, 11)
START = date(2026, 11, 1)


def row(day='2026-11-11', price=101.):
    d = date.fromisoformat(day)
    stamp = datetime(d.year, d.month, d.day, 16, tzinfo=ZoneInfo('America/New_York')).timestamp() * 1000
    return {'date': int(d.strftime('%Y%m%d')), 'time_key': stamp,
            'open': price, 'high': price + 1, 'low': price - 1, 'close': price, 'volume': 1000}


def response(rows=None):
    return {'ret_code': 0, 'data': {'kline_list': [row()] if rows is None else rows}}


class Reply:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.payload).encode()


def frames():
    index = pd.bdate_range(end='2026-11-11', periods=300)
    raw = pd.DataFrame({'Open': 100., 'High': 101., 'Low': 99., 'Close': 100., 'Volume': 1000.}, index=index)
    adjusted = raw.copy()
    adjusted.loc[:, source.OHLC] *= .9
    history = adjusted.iloc[:-1].copy()
    history['Raw Close'] = raw.iloc[:-1]['Close']
    return history, raw, adjusted


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime.fromisoformat('2026-11-12T10:30:00+08:00')
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class FutuSourceTests(TestCase):
    def test_ed25519_signature_matches_exact_sent_query(self):
        key = ed25519.Ed25519PrivateKey.generate()
        transport = Mock(return_value=Reply(response()))
        client = source.FutuClient('DUMMY', key, transport=transport)
        result = client.daily_bars(START, END, 1)
        request = transport.call_args.args[0]
        headers = {k.lower(): v for k,v in request.header_items()}
        url = urllib.parse.urlsplit(request.full_url)
        payload = f"{headers['x-timestamp']}\nGET\n{url.path}\n{url.query}\n".encode()
        key.public_key().verify(base64.b64decode(headers['authorization']), payload)
        self.assertEqual(headers['x-api-key'], 'DUMMY')
        self.assertRegex(headers['x-nonce'], r'^[A-Za-z0-9_-]{1,64}$')
        self.assertEqual(urllib.parse.parse_qs(url.query)['ktype'], ['2'])
        self.assertEqual(urllib.parse.parse_qs(url.query)['extended_time'], ['0'])
        self.assertEqual(url.hostname, 'webapi.futunn.com')
        self.assertEqual(url.path, source.PATH)
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(result.index[-1], pd.Timestamp(END))

    def test_rsa_signature_is_pkcs1_sha256(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        transport = Mock(return_value=Reply(response()))
        source.FutuClient('DUMMY', key, transport=transport).daily_bars(START, END, 0)
        request = transport.call_args.args[0]
        headers = {k.lower(): v for k,v in request.header_items()}
        url = urllib.parse.urlsplit(request.full_url)
        signed = f"{headers['x-timestamp']}\nGET\n{url.path}\n{url.query}\n".encode()
        key.public_key().verify(base64.b64decode(headers['authorization']), signed, padding.PKCS1v15(), hashes.SHA256())

    def test_authorization_and_rate_errors_are_sanitized(self):
        for code, status in [(401, 'authorization_failed'), (403, 'authorization_failed'), (429, 'rate_limited'), (500, 'provider_http_error')]:
            transport = Mock(side_effect=urllib.error.HTTPError('https://example.test/SECRET', code, 'SECRET_TOKEN', {}, io.BytesIO(b'SECRET_TOKEN')))
            client = source.FutuClient('SECRET_TOKEN', ed25519.Ed25519PrivateKey.generate(), transport=transport)
            with self.assertRaisesRegex(source.SourceError, '^' + status + '$') as caught:
                client.daily_bars(START, END, 0)
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(transport.call_count, 1)

    def test_provider_application_error_does_not_become_a_bar(self):
        client = source.FutuClient('DUMMY', ed25519.Ed25519PrivateKey.generate(), transport=Mock(return_value=Reply({'code': -12006, **{'data': response()['data']}})))
        with self.assertRaisesRegex(source.SourceError, '^provider_rejected$'):
            client.daily_bars(START, END, 0)

    def test_missing_credentials_never_attempts_http(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(source.urllib.request, 'build_opener') as network:
            with self.assertRaisesRegex(source.SourceError, '^authorization_required$'):
                source.FutuClient.from_environment()
        network.assert_not_called()

    def test_encrypted_key_configuration_and_bad_password(self):
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b'DUMMY_PASSWORD')).decode()
        config = {'FUTU_API_KEY': 'DUMMY', 'FUTU_PRIVATE_KEY_PEM': pem, 'FUTU_PRIVATE_KEY_PASSWORD': 'DUMMY_PASSWORD'}
        with patch.dict(os.environ, config, clear=True):
            self.assertIsInstance(source.FutuClient.from_environment(), source.FutuClient)
        config['FUTU_PRIVATE_KEY_PASSWORD'] = 'WRONG'
        with patch.dict(os.environ, config, clear=True):
            with self.assertRaisesRegex(source.SourceError, '^private_key_invalid$'):
                source.FutuClient.from_environment()

    def test_redirect_cannot_receive_credentials(self):
        with self.assertRaisesRegex(source.SourceError, '^unexpected_redirect$'):
            source._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.test')

    def test_invalid_daily_rows_are_rejected(self):
        cases = []
        missing = row(); missing['close'] = None; cases.append((missing, 'schema_invalid'))
        infinite = row(); infinite['close'] = np.inf; cases.append((infinite, 'price_invalid'))
        wrong = row(); wrong['high'] = 90.; cases.append((wrong, 'ohlc_invalid'))
        future = row('2026-11-12'); cases.append((future, 'session_invalid'))
        weekend = row('2026-11-07'); cases.append((weekend, 'session_invalid'))
        mismatch = row(); mismatch['time_key'] -= 86400 * 1000; cases.append((mismatch, 'session_invalid'))
        volume = row(); volume['volume'] = -1; cases.append((volume, 'volume_invalid'))
        for bad, status in cases:
            with self.subTest(status=status), self.assertRaisesRegex(source.SourceError, '^' + status + '$'):
                source.parse_daily_bars(response([bad]), START, END)

    def test_empty_or_duplicate_rows_are_rejected(self):
        with self.assertRaisesRegex(source.SourceError, '^no_data$'):
            source.parse_daily_bars(response([]), START, END)
        with self.assertRaisesRegex(source.SourceError, '^duplicate_session$'):
            source.parse_daily_bars(response([row(), row()]), START, END)

    def repair(self, history, raw, adjusted, **kwargs):
        client = Mock()
        client.daily_bars.side_effect = [raw, adjusted]
        result, report = source.repair_qqq_history(history, END, lambda d: d.weekday() < 5, client_factory=lambda: client, **kwargs)
        return result, report, client

    def test_verified_adjusted_patch_keeps_every_old_value_and_uses_raw_volume(self):
        history, raw, adjusted = frames()
        raw.loc[pd.Timestamp(END), source.OHLC] = [110., 111., 109., 110.]
        adjusted.loc[pd.Timestamp(END), source.OHLC] = [99., 100., 98., 99.]
        result, report, client = self.repair(history, raw, adjusted)
        self.assertEqual(report['status'], 'repaired')
        self.assertEqual(report['patched_dates'], ['2026-11-11'])
        pd.testing.assert_frame_equal(result.loc[history.index], history, check_freq=False)
        self.assertEqual(result.loc['2026-11-11', 'Close'], 99.)
        self.assertEqual(result.loc['2026-11-11', 'Raw Close'], 110.)
        self.assertEqual(client.daily_bars.call_args_list[0].args[2], 0)
        self.assertEqual(client.daily_bars.call_args_list[1].args[2], 1)

    def test_unadjusted_substitution_is_rejected_without_mutating_history(self):
        history, raw, adjusted = frames()
        original = history.copy(deep=True)
        result, report, _ = self.repair(history, raw, raw)
        self.assertEqual(report['status'], 'adjusted_price_basis_mismatch')
        pd.testing.assert_frame_equal(result, original)
        pd.testing.assert_frame_equal(history, original)

    def test_raw_close_disagreement_is_rejected(self):
        history, raw, adjusted = frames()
        raw.loc[history.index[-1], 'Close'] += 1
        _, report, _ = self.repair(history, raw, adjusted)
        self.assertEqual(report['status'], 'raw_price_mismatch')

    def test_new_dividend_basis_change_without_yahoo_adjustment_is_rejected(self):
        history, raw, adjusted = frames()
        adjusted.loc[:, source.OHLC] *= .99
        _, report, _ = self.repair(history, raw, adjusted)
        self.assertEqual(report['status'], 'adjusted_price_basis_mismatch')

    def test_too_few_anchors_cannot_validate_a_patch(self):
        history, raw, adjusted = frames()
        _, report, _ = self.repair(history, raw.iloc[-10:], adjusted.iloc[-10:])
        self.assertEqual(report['status'], 'overlap_insufficient')

    def test_missing_target_day_does_not_partially_commit(self):
        history, raw, adjusted = frames()
        result, report, _ = self.repair(history, raw.iloc[:-1], adjusted.iloc[:-1])
        self.assertEqual(report['status'], 'missing_completed_session')
        pd.testing.assert_frame_equal(result, history)

    def test_matching_raw_adjusted_dates_and_exchange_calendar_required(self):
        history, raw, adjusted = frames()
        _, report, _ = self.repair(history, raw.iloc[:-1], adjusted)
        self.assertEqual(report['status'], 'adjustment_sessions_mismatch')
        client = Mock(); client.daily_bars.side_effect = [raw, adjusted]
        _, report = source.repair_qqq_history(history, END, lambda d: d.weekday() < 5 and d != date(2026, 11, 10), client_factory=lambda: client)
        self.assertEqual(report['status'], 'session_invalid')

    def test_healthy_yahoo_does_not_use_futu(self):
        history, raw, adjusted = frames()
        factory = Mock()
        result, report = source.repair_qqq_history(adjusted, END, lambda d: True, client_factory=factory)
        factory.assert_not_called()
        pd.testing.assert_frame_equal(result, adjusted)
        self.assertEqual(report['status'], 'not_needed')

    def test_unconfigured_source_preserves_stale_history(self):
        history, _, _ = frames()
        with patch.dict(os.environ, {}, clear=True):
            result, report = source.repair_qqq_history(history, END, lambda d: d.weekday() < 5)
        self.assertEqual(report['status'], 'authorization_required')
        pd.testing.assert_frame_equal(result, history)

    def test_long_gap_or_short_primary_history_is_not_rebuilt_from_370_bars(self):
        history, _, _ = frames()
        for data, expected in [(history.iloc[:-10], 'gap_outside_repair_limit'), (history.iloc[-100:], 'primary_history_insufficient')]:
            factory = Mock()
            _, report = source.repair_qqq_history(data, END, lambda d: d.weekday() < 5, client_factory=factory)
            self.assertEqual(report['status'], expected)
            factory.assert_not_called()

    def test_pipeline_verified_patch_removes_stale_without_changing_signal_rules(self):
        history, raw, adjusted = frames()
        # bdate_range 本身不排股市休市；端到端 fixture 也須符合正式交易日曆。
        holidays = pd.to_datetime(['2025-11-27', '2025-12-25', '2026-01-01', '2026-01-19',
                                  '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19',
                                  '2026-07-03', '2026-09-07'])
        history = history.drop(holidays, errors='ignore')
        raw = raw.drop(holidays, errors='ignore')
        adjusted = adjusted.drop(holidays, errors='ignore')
        repaired = adjusted.copy(); repaired['Raw Close'] = raw['Close']
        factory = Mock(); factory.return_value.daily_bars.side_effect = [raw, adjusted]
        with patch.object(dashboard, 'datetime', FixedClock), patch.object(dashboard.yf, 'download', return_value=history), \
             patch.object(dashboard, 'fetch_yahoo_direct', return_value=history), \
             patch.object(source.FutuClient, 'from_environment', factory):
            result = dashboard.fetch_robust_market_data('QQQ')
        self.assertFalse(result.attrs['is_stale'])
        self.assertEqual(result.attrs['last_bar_date'], '2026-11-11')
        self.assertEqual(result.attrs['source_layer'], 'yahoo_direct+futu')
        pd.testing.assert_frame_equal(result, repaired, check_freq=False, check_flags=False)

    def test_pipeline_without_authorization_stays_stale_and_preserves_last_close(self):
        history, _, _ = frames()
        with patch.dict(os.environ, {}, clear=True), patch.object(dashboard, 'datetime', FixedClock), \
             patch.object(dashboard.yf, 'download', return_value=history), \
             patch.object(dashboard, 'fetch_yahoo_direct', return_value=history):
            result = dashboard.fetch_robust_market_data('QQQ')
        self.assertTrue(result.attrs['is_stale'])
        self.assertEqual(result.attrs['secondary_source']['status'], 'authorization_required')
        self.assertEqual(result.index[-1], pd.Timestamp('2026-11-10'))
        self.assertEqual(result['Close'].iloc[-1], history['Close'].iloc[-1])

    def test_multiple_missing_sessions_are_all_or_nothing(self):
        history, raw, adjusted = frames()
        history = history.iloc[:-2]
        raw = raw.drop(pd.Timestamp('2026-11-10'))
        adjusted = adjusted.drop(pd.Timestamp('2026-11-10'))
        result, report, _ = self.repair(history, raw, adjusted)
        self.assertEqual(report['status'], 'missing_completed_session')
        pd.testing.assert_frame_equal(result, history)


if __name__ == '__main__':
    main()
