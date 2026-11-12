"""QQQ 收盤缺日備援。只讀富途日 K；重疊價未通過驗證時不補價。"""
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import base64
import json
import math
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

HOST = 'https://webapi.futunn.com'
PATH = '/api/v1.0/quote/US.QQQ/history-kline'
MIN_OVERLAP = 60
MAX_GAP_SESSIONS = 5
PRICE_ATOL = .01
PRICE_RTOL = 1e-6
OHLC = ['Open', 'High', 'Low', 'Close']


class SourceError(Exception):
    """只攜帶可公開的狀態碼；不回傳 request、金鑰或供應商原文。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError('unexpected_redirect')


class FutuClient:
    """適合排程的 AppKey 簽章；只提供 QQQ 歷史日 K GET。"""
    def __init__(self, api_key, private_key, *, ssl_context=None, transport=None):
        self._api_key = api_key
        self._private_key = private_key
        if transport is None:
            if ssl_context is None:
                import certifi
                ssl_context = ssl.create_default_context(cafile=certifi.where())
            if not ssl_context.check_hostname or ssl_context.verify_mode != ssl.CERT_REQUIRED:
                raise SourceError('tls_verification_required')
            transport = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_context), _NoRedirect()).open
        self._transport = transport

    @classmethod
    def from_environment(cls, *, ssl_context=None):
        api_key = os.environ.get('FUTU_API_KEY', '').strip()
        pem = os.environ.get('FUTU_PRIVATE_KEY_PEM', '').strip()
        key_file = os.environ.get('FUTU_PRIVATE_KEY_FILE', '').strip()
        if not api_key and not pem and not key_file:
            raise SourceError('authorization_required')
        if not api_key or not (pem or key_file) or (pem and key_file):
            raise SourceError('credential_configuration_invalid')
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
            key_bytes = pem.encode('utf-8') if pem else Path(key_file).read_bytes()
            password = os.environ.get('FUTU_PRIVATE_KEY_PASSWORD', '').encode('utf-8') or None
            key = load_pem_private_key(key_bytes, password=password)
            if not isinstance(key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
                raise ValueError('unsupported key')
            if isinstance(key, rsa.RSAPrivateKey) and key.key_size < 2048:
                raise ValueError('weak key')
        except Exception:
            raise SourceError('private_key_invalid') from None
        return cls(api_key, key, ssl_context=ssl_context)

    def daily_bars(self, start, end, adjustment):
        if adjustment not in (0, 1):
            raise SourceError('adjustment_invalid')
        query = urllib.parse.urlencode([
            ('start', start.isoformat()), ('end', end.isoformat()),
            ('ktype', 2), ('autype', adjustment), ('num', 370), ('extended_time', 0),
        ])
        timestamp = str(time.time_ns() // 1_000_000)
        signed = f'{timestamp}\nGET\n{PATH}\n{query}\n'.encode('utf-8')
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding
        if isinstance(self._private_key, ed25519.Ed25519PrivateKey):
            signature = self._private_key.sign(signed)
        else:
            signature = self._private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        request = urllib.request.Request(HOST + PATH + '?' + query, headers={
            'X-Api-Key': self._api_key, 'X-Timestamp': timestamp,
            'X-Nonce': secrets.token_urlsafe(18),
            'Authorization': base64.b64encode(signature).decode('ascii'),
            'Accept': 'application/json', 'User-Agent': 'DynamicLeverage-EOD/1.0',
        }, method='GET')
        try:
            with self._transport(request, timeout=12) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 439):
                raise SourceError('authorization_failed') from None
            if error.code == 429:
                raise SourceError('rate_limited') from None
            raise SourceError('provider_http_error') from None
        except SourceError:
            raise
        except Exception:
            raise SourceError('provider_unavailable') from None
        if not isinstance(payload, dict):
            raise SourceError('schema_invalid')
        if any(payload[key] != 0 for key in ('code', 'ret_code') if key in payload):
            raise SourceError('provider_rejected')
        return parse_daily_bars(payload, start, end)


def parse_daily_bars(payload, start, end):
    """驗證回應是完整、同交易日且範圍內的日 K；不接受盤後最後成交代替收盤。"""
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('kline_list'), list):
        raise SourceError('schema_invalid')
    rows = data['kline_list']
    if not rows:
        raise SourceError('no_data')
    precision = data.get('volume_precision', 0)
    if type(precision) is not int or not 0 <= precision <= 9:
        raise SourceError('schema_invalid')
    parsed = {}
    try:
        for row in rows:
            day_text = str(row['date'])
            if len(day_text) != 8 or not day_text.isdigit():
                raise SourceError('session_invalid')
            day = datetime.strptime(day_text, '%Y%m%d').date()
            if not start <= day <= end or day.weekday() >= 5:
                raise SourceError('session_invalid')
            stamp = float(row['time_key'])
            if not math.isfinite(stamp) or datetime.fromtimestamp(stamp / 1000, ZoneInfo('America/New_York')).date() != day:
                raise SourceError('session_invalid')
            if day in parsed:
                raise SourceError('duplicate_session')
            values = {key: float(row[key.lower()]) for key in OHLC}
            if not all(math.isfinite(v) and v > 0 for v in values.values()):
                raise SourceError('price_invalid')
            if not (values['Low'] <= min(values['Open'], values['Close']) <= max(values['Open'], values['Close']) <= values['High']):
                raise SourceError('ohlc_invalid')
            volume = float(row['volume']) / 10 ** precision
            if not math.isfinite(volume) or volume < 0:
                raise SourceError('volume_invalid')
            parsed[day] = {**values, 'Volume': volume}
    except SourceError:
        raise
    except Exception:
        raise SourceError('schema_invalid') from None
    result = pd.DataFrame.from_dict(parsed, orient='index').sort_index()
    result.index = pd.DatetimeIndex(result.index)
    return result


def repair_qqq_history(history, expected_session, is_session, *, client_factory=None, ssl_context=None):
    """只修最後完整列之後最多五個交易日；不重設十年歷史或覆蓋有效舊列。"""
    report = {'provider': 'futu', 'status': 'not_needed', 'patched_dates': []}
    result = history.copy(deep=True)
    try:
        prices = result[OHLC].apply(pd.to_numeric, errors='coerce').to_numpy(dtype=float)
        valid = np.isfinite(prices).all(axis=1) & (prices > 0).all(axis=1)
        closed = result.index[valid & (result.index <= pd.Timestamp(expected_session))]
        if len(closed) < 225:
            raise SourceError('primary_history_insufficient')
        last = closed.max().date()
        if last >= expected_session:
            return result, report
        missing = []
        day = last + timedelta(days=1)
        while day <= expected_session:
            if is_session(day):
                missing.append(pd.Timestamp(day))
            day += timedelta(days=1)
        if not missing or len(missing) > MAX_GAP_SESSIONS:
            raise SourceError('gap_outside_repair_limit')
        client = client_factory() if client_factory else FutuClient.from_environment(ssl_context=ssl_context)
        start = expected_session - timedelta(days=500)  # <=358 weekdays, below the 370-bar cap.
        raw = client.daily_bars(start, expected_session, 0)
        adjusted = client.daily_bars(start, expected_session, 1)
        if not raw.index.equals(adjusted.index):
            raise SourceError('adjustment_sessions_mismatch')
        if any(not is_session(day.date()) for day in raw.index):
            raise SourceError('session_invalid')
        common = closed.intersection(adjusted.index)
        if len(common) < MIN_OVERLAP:
            raise SourceError('overlap_insufficient')
        # 不用單一錨點或平均倍率硬湊；四價直接比對現行還原價口徑。
        baseline = result.loc[common, OHLC].to_numpy(dtype=float)
        candidate = adjusted.loc[common, OHLC].to_numpy(dtype=float)
        report['overlap_sessions'] = len(common)
        report['max_ohlc_difference'] = float(np.max(np.abs(baseline - candidate)))
        if not np.isclose(baseline, candidate, atol=PRICE_ATOL, rtol=PRICE_RTOL).all():
            raise SourceError('adjusted_price_basis_mismatch')
        if 'Raw Close' in result.columns:
            raw_baseline = pd.to_numeric(result.loc[common, 'Raw Close'], errors='coerce').to_numpy(dtype=float)
            raw_good = np.isfinite(raw_baseline) & (raw_baseline > 0)
            if raw_good.any() and not np.isclose(raw_baseline[raw_good], raw.loc[common, 'Close'].to_numpy(dtype=float)[raw_good], atol=PRICE_ATOL, rtol=PRICE_RTOL).all():
                raise SourceError('raw_price_mismatch')
        if any(day not in adjusted.index for day in missing):
            raise SourceError('missing_completed_session')
        # 全數通過後才一次提交補價；任何拒絕路徑都回傳完整原表。
        patched = result.copy(deep=True)
        if 'Raw Close' not in patched.columns:
            patched['Raw Close'] = np.nan
        for day in missing:
            for col in OHLC:
                patched.loc[day, col] = adjusted.loc[day, col]
            patched.loc[day, 'Volume'] = raw.loc[day, 'Volume']
            patched.loc[day, 'Raw Close'] = raw.loc[day, 'Close']
            if 'Adj Close' in patched.columns:
                patched.loc[day, 'Adj Close'] = adjusted.loc[day, 'Close']
        report.update(status='repaired', patched_dates=[day.date().isoformat() for day in missing])
        return patched.sort_index(), report
    except SourceError as error:
        report['status'] = str(error)
    except Exception:
        report['status'] = 'validation_failed'
    return result, report
