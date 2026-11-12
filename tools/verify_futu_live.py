"""雲端唯讀驗收：真實授權、日 K 口徑及記憶體缺日重播；不建置／通知。"""
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
import app as dashboard
from futu_source import FutuClient, SourceError, OHLC, PRICE_ATOL, PRICE_RTOL


def public_response_sample(body):
    """只留下錯誤數字與公開QQQ欄位，從不輸出HTTP headers或任意錯誤原文。"""
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return {'root_type': type(payload).__name__}
        out = {key: payload[key] for key in ('code', 'ret_code') if type(payload.get(key)) is int}
        data = payload.get('data')
        out['data_type'] = type(data).__name__
        rows = data.get('kline_list') if isinstance(data, dict) else None
        out['kline_list_type'] = type(rows).__name__
        if isinstance(rows, list):
            out['rows'] = len(rows)
            samples = []
            for row in rows[:1] + rows[-1:]:
                if not isinstance(row, dict):
                    continue
                sample = {}
                for key in ('date', 'time_key', 'time_zone', 'open', 'high', 'low', 'close', 'volume'):
                    value = row.get(key)
                    if type(value) is float and not math.isfinite(value):
                        sample[key] = '[non_finite]'
                    elif value is None or type(value) in (int, float):
                        sample[key] = value
                    elif isinstance(value, str) and re.fullmatch(r'[0-9\s:./+\-TZ]{1,40}', value):
                        sample[key] = value
                    else:
                        sample[key] = '[unexpected_type]'
                samples.append(sample)
            out['public_bar_samples'] = samples
        return out
    except Exception:
        return {'response': 'not_json'}


class BodyReply:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = {'mode': 'live_read_only_with_in_memory_last_bar_holdout',
              'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'symbol': 'US.QQQ',
              'credentials_present': {name: bool(os.environ.get(name, '').strip()) for name in
                  ('FUTU_API_KEY', 'FUTU_PRIVATE_KEY_PEM', 'FUTU_PRIVATE_KEY_PASSWORD')},
              'responses': [], 'all_passed': False, 'stage': 'credentials',
              'dashboard_deployed': False, 'telegram_sent': False}
    try:
        client = FutuClient.from_environment()
        report['private_key_loaded'] = True
        original_transport = client._transport

        def observe(request, timeout):
            kind = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query).get('autype', ['unknown'])[0]
            try:
                with original_transport(request, timeout=timeout) as response:
                    body = response.read()
                report['responses'].append({'adjustment': kind, 'http_status': 200, **public_response_sample(body)})
                return BodyReply(body)
            except urllib.error.HTTPError as error:
                report['responses'].append({'adjustment': kind, 'http_status': error.code, **public_response_sample(error.read())})
                raise

        client._transport = observe
        report['stage'] = 'primary_history'
        with patch.object(FutuClient, 'from_environment', return_value=client):
            primary = dashboard.fetch_robust_market_data('QQQ')
        if primary is None:
            raise SourceError('primary_history_unavailable')
        end = date.fromisoformat(primary.attrs['expected_session'])
        report.update(expected_session=end.isoformat(), primary_last_date=primary.index[-1].date().isoformat(),
                      primary_source=primary.attrs.get('source_layer'))
        start = end - timedelta(days=500)
        report['stage'] = 'futu_raw'
        raw = client.daily_bars(start, end, 0)
        report['stage'] = 'futu_adjusted'
        adjusted = client.daily_bars(start, end, 1)
        report.update(api_access_verified=True, raw_sessions=len(raw), adjusted_sessions=len(adjusted),
                      futu_last_date=adjusted.index[-1].date().isoformat())
        if not raw.index.equals(adjusted.index) or pd.Timestamp(end) not in adjusted.index:
            raise SourceError('missing_completed_session')

        # Yahoo完整收盤在記憶體移除一天；真實富途回覆保留不變，不修改正式快取。
        report['stage'] = 'in_memory_gap_replay'
        truncated = primary.loc[primary.index < pd.Timestamp(end)].copy(deep=True)
        class CapturedClient:
            def daily_bars(self, requested_start, requested_end, adjustment):
                return (raw if adjustment == 0 else adjusted).copy(deep=True)
        with patch.object(dashboard.yf, 'download', return_value=truncated), \
             patch.object(dashboard, 'fetch_yahoo_direct', return_value=truncated), \
             patch.object(FutuClient, 'from_environment', return_value=CapturedClient()):
            replay = dashboard.fetch_robust_market_data('QQQ')
        report['repair'] = replay.attrs.get('secondary_source') if replay is not None else None
        if replay is None or replay.attrs.get('is_stale') or (report['repair'] or {}).get('status') != 'repaired':
            raise SourceError('price_alignment_or_gap_repair_failed')

        original_patches = (primary.attrs.get('secondary_source') or {}).get('patched_dates', [])
        if pd.Timestamp(end) in primary.index and end.isoformat() not in original_patches:
            baseline = primary.loc[pd.Timestamp(end), OHLC].to_numpy(dtype=float)
            rebuilt = replay.loc[pd.Timestamp(end), OHLC].to_numpy(dtype=float)
            report['heldout_yahoo_max_ohlc_difference'] = float(np.max(np.abs(baseline - rebuilt)))
            if not np.isclose(baseline, rebuilt, atol=PRICE_ATOL, rtol=PRICE_RTOL).all():
                raise SourceError('heldout_price_mismatch')
        else:
            report['heldout_yahoo_comparison'] = 'no_independent_target_bar_available'
        report.update(stage='completed', status='validated', all_passed=True)
    except SourceError as error:
        report['status'] = str(error)
    except Exception as error:
        report.update(status='unexpected_error', exception_type=type(error).__name__)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['all_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
