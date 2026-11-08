import os
import sys
import json
import math
import shutil
import argparse
from html import escape
import urllib.request
import urllib.error
from datetime import datetime

# 確保輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def compute_signal(**kwargs):
    # Notification-only runs do not need to import the market-data application.
    from app import compute_signal as calculate
    return calculate(**kwargs)


def compute_rollover_radar():
    from app import compute_rollover_radar as calculate
    return calculate()


def _market_events(signal):
    """Stable event identity uses the event's date, never snapshot age or bar index."""
    events = {}
    for kind, field in (('signal_change', 'last_signal_change'), ('excess_trim', 'last_excess_trim')):
        if field in signal:
            candidates = [signal[field]]  # Explicit null means no such event in the new API.
        else:
            candidates = [signal.get('last_action'), signal.get('last_trim')]
        for event in candidates:
            if not isinstance(event, dict):
                continue
            is_excess = '+0.15' in str(event.get('type', ''))
            if is_excess != (kind == 'excess_trim'):
                continue
            try:
                event_date = event['date']
                datetime.strptime(event_date, '%Y-%m-%d')
                before, after = round(float(event['from_lev']), 2), round(float(event['to_lev']), 2)
                if not math.isfinite(before) or not math.isfinite(after) or before == after:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            events[kind] = {
                **event,
                'event_id': f"{signal['ticker']}|{kind}|{event_date}|{before:.2f}|{after:.2f}",
            }
            break
        if kind == 'signal_change' and field not in signal and kind not in events:
            try:
                before, after = (round(float(signal[key]), 2) for key in ('target_yesterday', 'target_today'))
                if math.isfinite(before) and math.isfinite(after) and before != after:
                    events[kind] = {
                        'date': signal['date'], 'from_lev': before, 'to_lev': after,
                        'type': '目標訊號變更',
                        'event_id': f"{signal['ticker']}|{kind}|{signal['date']}|{before:.2f}|{after:.2f}",
                    }
            except (KeyError, TypeError, ValueError):
                pass
    return events


def _freshness_issues(signal):
    """Keep stale dates, missing markets (validator), and cached fallback distinct."""
    label = {'QQQ': '美股', '006208.TW': '台股'}.get(signal.get('ticker'), signal.get('ticker', '市場'))
    status = str(signal.get('data_status') or '')
    date, expected = signal.get('date'), signal.get('expected_session')
    dated_lag = isinstance(date, str) and isinstance(expected, str) and date < expected
    stale = signal.get('is_stale') or status in ('stale', 'fallback_stale') or dated_lag
    fallback = signal.get('fallback_used') or status.startswith('fallback')
    short = lambda value: value[5:].replace('-', '/') if isinstance(value, str) and len(value) == 10 else (value or '未知')
    issues = []
    if dated_lag:
        issues.append(f"{label}資料延遲：{short(date)}，應{short(expected)}")
    elif stale and not fallback:
        issues.append(f"{label}資料新鮮度未確認（資料{short(date)}，應有日期待核）")
    if fallback:
        issues.append(f"{label}來源失敗，使用備援資料（截至{short(date)}）")
    if status not in ('', 'ok', 'stale', 'fallback', 'fallback_stale'):
        issues.append(f"{label}資料來源狀態：{status}")
    return issues


def _strategy_state_text(signal):
    state = signal.get('strategy_state')
    if not isinstance(state, dict) or state.get('schema_version') != 1:
        return '冷卻資訊未提供（舊版資料）'
    parts = [state.get('label', '策略狀態')]
    cd = state.get('cooldown') or {}
    if cd.get('active'):
        parts.append(f"{_cooldown_direction(cd)}，餘 {cd.get('remaining_days')} 個市場交易日")
    elif cd.get('timer_active') and not cd.get('applies_to_current_stage'):
        parts.append('防禦階段，階梯冷卻不適用')
    else:
        parts.append('階梯冷卻已結束')
    if cd.get('step_event_on_data_bar'):
        parts.append('資料日階梯動作已生效，新冷卻自該動作起算')
    grace = state.get('grace') or {}
    if grace.get('active'):
        parts.append(f"年線容忍 {grace.get('days_elapsed')}/{grace.get('days_allowed')} 日")
    if (state.get('d1') or {}).get('blocked'):
        parts.append('D1 僅凍結升檔')
    if (state.get('bull_acceleration') or {}).get('active'):
        parts.append(f"牛初加速（含資料日餘 {(state['bull_acceleration']).get('days_including_data_bar')} 個市場交易日）")
    if (state.get('extension') or {}).get('capping_active'):
        parts.append(f"Extension 封頂 {state['extension'].get('cap')}x")
    if (state.get('priority_defense') or {}).get('bypasses_step_cooldown'):
        parts.append('年線／深熊優先防禦不受階梯冷卻限制')
    return '；'.join(parts)


def _cooldown_direction(cd):
    try:
        before, after = float(cd['last_step_from']), float(cd['last_step_to'])
        if math.isfinite(before) and math.isfinite(after):
            if after < before:
                return '減碼後階梯冷卻中'
            if after > before:
                return '加碼後階梯冷卻中'
    except (KeyError, TypeError, ValueError):
        pass
    return {'step_up': '加碼後階梯冷卻中', 'step_down': '減碼後階梯冷卻中'}.get(
        cd.get('last_step_event_kind'), '階梯冷卻中')


def _channel_state_text(signal, action):
    state = signal.get('strategy_state')
    if not isinstance(state, dict) or state.get('schema_version') != 1 or not isinstance(state.get('cooldown'), dict):
        return '冷卻資訊未提供'
    cd = state.get('cooldown') or {}
    reasons = [item['label'] for item in state.get('blocked_actions', [])
               if item.get('action') == action and item.get('label')
               and not (cd.get('active') and item.get('reason') == 'cooldown')]
    if cd.get('active'):
        reasons.insert(0, f"{_cooldown_direction(cd)}，餘 {cd.get('remaining_days')} 個市場交易日")
    else:
        reasons.insert(0, '冷卻已結束' if cd.get('applies_to_current_stage') else '防禦轉換不受階梯冷卻限制')
    if (state.get('triggers') or {}).get('breakout_up' if action == 'step_up' else 'breakout_down'):
        reasons.append('資料日已突破通道' if action == 'step_up' else '資料日已跌破通道')
    return '；'.join(dict.fromkeys(reasons))


def _snapshot_signature(signals, rollover):
    return json.dumps({
        'signals': [
            {
                'ticker': s['ticker'], 'date': s.get('date'),
                'target_today': round(float(s['target_today']), 2),
                'target_yesterday': round(float(s['target_yesterday']), 2),
                'is_stale': bool(s.get('is_stale', False)),
                'data_status': s.get('data_status') or ('stale' if s.get('is_stale') else 'ok'),
                'expected_session': s.get('expected_session'),
                'fallback_used': bool(s.get('fallback_used')),
                'current_events': sorted(e['event_id'] for e in _market_events(s).values()
                                         if e['date'] == s.get('date')),
            }
            for s in sorted(signals, key=lambda item: item['ticker'])
        ],
        'rollover': {k: (rollover.get(k) or {}).get('days_left')
                     if isinstance(rollover.get(k), dict) else rollover.get(k) for k in ('tw', 'us')},
    }, ensure_ascii=False, sort_keys=True)


def _read_notification_state(path):
    state = {'schema_version': 2, 'markets': {}, 'last_snapshot_sig': None, 'last_event_sig': None}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    if raw.get('schema_version') == 2:
        raw_markets = raw.get('markets') if isinstance(raw.get('markets'), dict) else {}
        for ticker, kinds in raw_markets.items():
            if isinstance(kinds, dict):
                state['markets'][ticker] = {
                    kind: sorted({key for key in kinds.get(kind, []) if isinstance(key, str)})
                    for kind in ('signal_change', 'excess_trim')
                    if isinstance(kinds.get(kind, []), list)
                }
        if isinstance(raw.get('last_snapshot_sig'), str):
            state['last_snapshot_sig'] = raw['last_snapshot_sig']
            state['last_event_sig'] = raw['last_snapshot_sig']
        return state
    # Legacy snapshots were written only after delivery. Preserve identifiable events,
    # without inventing a sent event when the old state is missing or unparseable.
    try:
        legacy = json.loads(raw.get('last_event_sig') or 'null')
        if not isinstance(legacy, dict) or not isinstance(legacy.get('signals'), list):
            return state
        for signal in legacy['signals']:
            if not isinstance(signal, dict) or not signal.get('ticker'):
                continue
            kinds = state['markets'].setdefault(signal['ticker'], {})
            for kind, event in _market_events(signal).items():
                kinds[kind] = [event['event_id']]
        state['last_snapshot_sig'] = _snapshot_signature(legacy['signals'], legacy.get('rollover') or {})
    except (KeyError, TypeError, ValueError):
        # Any successfully identified legacy events above remain useful.
        pass
    return state


def _notify_snapshot(signals, rollover, state_path):
    valid, errors = validate_signals_payload(signals)
    if not valid:
        raise RuntimeError(f"Invalid notification payload: {'; '.join(errors)}")
    state = _read_notification_state(state_path)
    pending = {}
    for signal in signals:
        seen = state['markets'].get(signal['ticker'], {})
        pending[signal['ticker']] = {
            kind: event for kind, event in _market_events(signal).items()
            if not _freshness_issues(signal) and event['date'] == signal['date']
            and event['event_id'] not in seen.get(kind, [])
        }
    signature = _snapshot_signature(signals, rollover)
    if state['last_snapshot_sig'] == signature and not any(pending.values()):
        return {'ok': True, 'dedup_skipped': True, 'delivered': False,
                'status': 'dedup_skipped', 'attempts': 0}
    result = send_push_notification(signals, rollover, pending_events=pending)
    if not result.get('ok', False):
        raise RuntimeError(f"Telegram push failed: {result}")
    if result.get('delivered') is True and not result.get('skipped', False):
        for ticker, events in pending.items():
            kinds = state['markets'].setdefault(ticker, {})
            for kind, event in events.items():
                kinds[kind] = sorted(set(kinds.get(kind, [])) | {event['event_id']})
        state['last_snapshot_sig'] = signature
        state['last_event_sig'] = signature  # Compatibility with existing snapshot-state readers.
        write_json_atomic(state_path, state)
    return result


def format_push_summary(signals, rollover, pending_events):
    """只摘要本次尚未通知的動作；完整階段與歷史留在儀錶板。"""
    fresh_pending = {s['ticker']: {} if _freshness_issues(s) else pending_events.get(s['ticker'], {})
                     for s in signals}
    has_action = any(events for events in fresh_pending.values())
    degraded = any(_freshness_issues(s) for s in signals)
    rollover_urgent = any((rollover.get(k) or {}).get('days_left', 99) <= 2 for k in ('tw', 'us'))
    if degraded:
        header = '⚠️ <b>資料異常，請核對以下事項</b>'
    elif has_action:
        header = '🔔 <b>本次有待核對動作</b>'
    elif rollover_urgent:
        header = '🎯 <b>需確認換約</b>'
    else:
        header = '✅ <b>本次維持，無新增調倉</b>'
    lines = [header]
    for signal in signals:
        label = {'QQQ': '美股', '006208.TW': '台股'}.get(signal['ticker'], signal['ticker'])
        prefix = f"• {escape(label)} {signal['date'][5:].replace('-', '/')}："
        issues = _freshness_issues(signal)
        if issues:
            # 保留日期差異與來源失敗，過期事件也不消耗已送達紀錄。
            lines.append(prefix + '暫停調倉指示；' + escape('；'.join(issues)))
            continue
        events = fresh_pending[signal['ticker']]
        actions = []
        if events.get('signal_change'):
            event = events['signal_change']
            actions.append(f"目標 <b>{event['from_lev']:.2f}x → {event['to_lev']:.2f}x</b>（本次新訊號）")
        if events.get('excess_trim'):
            actions.append(f"模擬超標修剪提醒：實帳 &gt;{signal['trim_trigger_lev']:.2f}x 才降回 <b>{signal['target_today']:.2f}x</b>")
        if not actions:
            actions.append(f"維持 <b>{signal['target_today']:.2f}x</b>")
        state = signal.get('strategy_state') or {}
        cd = state.get('cooldown') or {}
        suffix = ''
        if state.get('schema_version') == 1 and cd.get('active'):
            direction = _cooldown_direction(cd).replace('階梯冷卻中', '冷卻')
            suffix = f"（{direction}餘 {cd.get('remaining_days')} 交易日）"
        lines.append(prefix + '；'.join(actions) + suffix)
    for key, label in (('tw', '台指期'), ('us', '那指期')):
        days = (rollover.get(key) or {}).get('days_left', 99)
        if days <= 5:
            reminder = '今日結算，立即確認換約' if days == 0 else (
                '已到結算硬期限，立即確認換約' if days < 0 else f'結算倒數 {days} 天，請確認換約')
            lines.append(f'🎯 {label}：{reminder}。')
    lines.append('實帳超過目標+0.15x仍須核對修剪；詳情看儀錶板。')
    return '\n'.join(lines), bool(has_action or degraded or rollover_urgent)


def send_push_notification(signals, rollover, pending_events=None):
    """Send a report; delivered means API acknowledgement, not a read receipt.

    Retries are bounded at three attempts. HTTP 429 retry_after backoff is not
    implemented; a lost acknowledgement can cause duplicate delivery. Neither
    this transport nor the persisted event log promises exactly-once delivery.
    """
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        print(f"ℹ️ 未偵測到完整 TG 設定 (TG_BOT_TOKEN: {'已設定' if bot_token else '未設定'}, TG_CHAT_ID: {'已設定' if chat_id else '未設定'})，略過 Telegram 推播。")
        return {"ok": True, "skipped": True, "delivered": False, "status": "skipped_unconfigured", "attempts": 0}

    # F03/F11: 嚴格驗證雙市場完整性、日期格式與槓桿邊界，拒絕單一市場或異常訊號發送推播
    is_valid, val_errors = validate_signals_payload(signals)
    if not is_valid:
        print(f"❌ 訊號列表未通過完整性驗證 ({'; '.join(val_errors)})，拒絕發送推播！")
        return {"ok": False, "skipped": False, "delivered": False, "status": "invalid_payload", "reason": "invalid_or_incomplete_signals", "errors": val_errors, "attempts": 0}

    if pending_events is None:
        pending_events = {s['ticker']: {kind: event for kind, event in _market_events(s).items()
                                      if not _freshness_issues(s) and event['date'] == s['date']} for s in signals}
    else:
        # A direct caller cannot re-enable an old event on a degraded market card.
        pending_events = {s['ticker']: {} if _freshness_issues(s) else pending_events.get(s['ticker'], {})
                          for s in signals}
    text, urgent = format_push_summary(signals, rollover, pending_events)

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": (not urgent)
    }).encode("utf-8")

    # F17: 結構化有限次重試、檢查 JSON ok:true 並回傳明確狀態
    max_retries = 3
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = resp.read().decode('utf-8')
                try:
                    resp_json = json.loads(resp_data)
                except Exception:
                    resp_json = {}
                if resp_json.get("ok") is not True:
                    last_error = f"Telegram API returned ok=false: {resp_data[:120]}"
                    print(f"❌ Telegram API 應用層拒絕 (第 {attempt} 次): {last_error}")
                    return {"ok": False, "skipped": False, "delivered": False, "status": "failed", "error": last_error, "attempts": attempt}
                print(f"✅ Telegram 戰情推播發送成功 (第 {attempt} 次嘗試)！API 回應: {resp_data[:80]}...")
                return {"ok": True, "skipped": False, "delivered": True, "status": "delivered", "attempts": attempt}
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode('utf-8', errors='ignore')
            last_error = f"HTTP {e.code}: {err_msg}"
            print(f"❌ Telegram 推播 HTTP 錯誤 (第 {attempt}/{max_retries} 次) [{e.code}]: {err_msg}")
        except Exception as e:
            last_error = str(e)
            print(f"❌ Telegram 推播發送失敗 (第 {attempt}/{max_retries} 次): {e}")

    return {"ok": False, "skipped": False, "delivered": False, "status": "failed", "error": last_error, "attempts": max_retries}


def validate_signals_payload(signals):
    """F03: 驗證雙市場完整性、欄位齊備、ISO 日期可解析與數值有限/合理性"""
    from datetime import datetime as _dt
    errors = []
    if not isinstance(signals, list) or len(signals) != 2:
        errors.append(f"Expected 2 market signals (QQQ and 006208.TW), got {0 if not isinstance(signals, list) else len(signals)}")
        return False, errors

    expected_tickers = {'QQQ', '006208.TW'}
    actual_tickers = {s.get('ticker') for s in signals if isinstance(s, dict)}
    if actual_tickers != expected_tickers:
        errors.append(f"Missing required tickers: expected {expected_tickers}, got {actual_tickers}")

    required_numeric = ['close', 'target_today', 'target_yesterday', 'trim_trigger_lev',
                        'sma_fast_val', 'sma_slow_val', 'high_bw', 'low_bw', 'step_idx', 'max_steps']
    for s in signals:
        if not isinstance(s, dict):
            errors.append("Signal entry is not a dict")
            continue
        tkr = s.get('ticker', 'UNKNOWN')
        d_str = s.get('date')
        if not d_str or not isinstance(d_str, str):
            errors.append(f"{tkr}: missing date")
        else:
            try:
                _dt.strptime(d_str, '%Y-%m-%d')
            except Exception:
                errors.append(f"{tkr}: unparseable ISO date '{d_str}'")
        for k in required_numeric:
            val = s.get(k)
            if val is None or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                errors.append(f"{tkr}: invalid or non-finite field '{k}'={val}")
        if (s.get('close', 0) or 0) <= 0 or (s.get('sma_slow_val', 0) or 0) <= 0 or (s.get('high_bw', 0) or 0) <= 0 or (s.get('low_bw', 0) or 0) <= 0:
            errors.append(f"{tkr}: non-positive price, channel, or slow MA")
        t_today = s.get('target_today', 0)
        t_yest = s.get('target_yesterday', 0)
        if not isinstance(t_today, (int, float)) or not isinstance(t_yest, (int, float)) or t_today <= 0 or t_today > 3.0 or t_yest <= 0 or t_yest > 3.0:
            errors.append(f"{tkr}: target leverage out of valid range (0, 3.0]: today={t_today}, yesterday={t_yest}")
        s_idx = s.get('step_idx', -1)
        m_steps = s.get('max_steps', -1)
        if not isinstance(s_idx, (int, float)) or not isinstance(m_steps, (int, float)) or s_idx < 0 or m_steps <= 0 or s_idx > m_steps:
            errors.append(f"{tkr}: invalid step_idx/max_steps ({s_idx}/{m_steps})")

    return len(errors) == 0, errors


def write_json_atomic(path, payload):
    import time as _time
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    for _retry_idx in range(5):
        try:
            os.replace(tmp_path, path)
            break
        except PermissionError:
            if _retry_idx == 4:
                raise
            _time.sleep(0.05)


def build(notify=True):
    print("🚀 開始建置靜態網站 (GitHub Pages)...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    
    os.makedirs(dist_dir, exist_ok=True)

    # 1. 運算市場信號
    print("📊 正在抓取並計算市場數據...")
    qqq = compute_signal(
        name="美股 QQQ", ticker="QQQ",
        base_leverage=0.8, max_leverage=3.0,
        fast_ma=5, slow_ma=220, breakout_window=8, breakout_up=8, breakout_dn=8, cooldown=8,
        allocs=[0.0, 0.65, 0.85, 1.0],
        grace_days=0,
        extension_guard={'threshold': 1.16, 'cap': 1.8},
        deep_bear_guard={'bear_ma': 60, 'ratio': 0.97, 'deep_base': 0.4, 'add_above_bear': True},
        add_above_bear=True,
        pre_bull_boost=1.0
    )
    
    tw = compute_signal(
        name="台股 006208", ticker="006208.TW",
        base_leverage=1.0, max_leverage=3.0,
        fast_ma=8, slow_ma=220, breakout_window=15, breakout_up=15, breakout_dn=25, cooldown=10,
        allocs=[0.0, 0.50, 0.75, 0.90, 1.0],
        grace_days=1,
        bull_acceleration={'enable': True, 'accel_base': 1.2, 'accel_days': 252, 'min_bear_days': 40},
        deep_bear_guard={'bear_ma': 60, 'ratio': 0.97, 'deep_base': 0.5},
        add_above_bear=False,
        pre_bull_boost=1.2
    )
    
    # F03: 嚴格要求雙市場皆非 None 才構成有效 signals，拒絕單市場缺漏或全空覆寫
    signals = [qqq, tw] if (qqq is not None and tw is not None) else []
    is_valid, val_errors = validate_signals_payload(signals)
    if not is_valid:
        err_summary = "; ".join(val_errors)
        print(f"❌ [F03 Gate] 雙市場訊號驗收未通過，拒絕寫入 api_data.json 與發送綠燈推播: {err_summary}")
        raise RuntimeError(f"Signal validation failed: {err_summary}")
    
    # 2. 運算期貨轉倉雷達
    print("🎯 正在計算轉倉雷達數據...")
    rollover = compute_rollover_radar()

    # 3. 原子寫入 JSON API 資料
    data_path = os.path.join(dist_dir, "api_data.json")
    write_json_atomic(data_path, signals)
    print(f"✅ 已原子輸出: {data_path}")

    rollover_path = os.path.join(dist_dir, "api_rollover.json")
    write_json_atomic(rollover_path, rollover)
    print(f"✅ 已原子輸出: {rollover_path}")

    # 4. 複製靜態資源
    static_src = os.path.join(base_dir, "static")
    static_dst = os.path.join(dist_dir, "static")
    shutil.copytree(static_src, static_dst, dirs_exist_ok=True)
    print(f"✅ 已複製 static 目錄到: {static_dst}")

    # 5. 處理 index.html (轉為相對路徑)
    html_src = os.path.join(base_dir, "templates", "index.html")
    with open(html_src, "r", encoding="utf-8") as f:
        html_content = f.read()

    html_content = html_content.replace('href="/static/', 'href="./static/')
    html_content = html_content.replace('src="/static/', 'src="./static/')

    html_dst = os.path.join(dist_dir, "index.html")
    tmp_html = html_dst + ".tmp"
    with open(tmp_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    os.replace(tmp_html, html_dst)
    print(f"✅ 已輸出 index.html 到: {html_dst}")

    # 6. 建立 .nojekyll 防止 GitHub 忽略特定檔案
    nojekyll_path = os.path.join(dist_dir, ".nojekyll")
    with open(nojekyll_path, "w") as f:
        pass
    print("✅ 已建立 .nojekyll")

    # S03: 保留既有推播事件去重紀錄（僅在檔案尚不存在時初始化，絕不於重複建置相同事件時清空已推播狀態）
    dedup_path = os.path.join(dist_dir, ".last_notified_event.json")
    if not os.path.exists(dedup_path):
        write_json_atomic(dedup_path, {"last_event_sig": None})

    # 7. 若啟用同步推播通知 (預設在本地單獨執行時可用；CI 部署時改由部署成功後執行 --notify-only)
    if notify:
        _notify_snapshot(signals, rollover, dedup_path)

    print(f"\n🎉 靜態建置完成！輸出目錄: {dist_dir}")
    print(f"🕒 建置時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def notify_from_dist():
    """F11/F17/S03/S04: 從已建置並部署完成之 dist/ 讀取訊號發送 Telegram 推播，僅在確認送達 (delivered=True) 時寫入去重狀態"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    data_path = os.path.join(dist_dir, "api_data.json")
    rollover_path = os.path.join(dist_dir, "api_rollover.json")
    dedup_path = os.path.join(dist_dir, ".last_notified_event.json")

    if not os.path.exists(data_path) or not os.path.exists(rollover_path):
        raise RuntimeError("Missing dist/api_data.json or dist/api_rollover.json for post-deploy notification")

    with open(data_path, "r", encoding="utf-8") as f:
        signals = json.load(f)
    with open(rollover_path, "r", encoding="utf-8") as f:
        rollover = json.load(f)

    return _notify_snapshot(signals, rollover, dedup_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build static dashboard and/or send EOD Telegram notification")
    parser.add_argument("--build-only", action="store_true", help="Only build dist/ artifacts without sending Telegram push")
    parser.add_argument("--notify-only", action="store_true", help="Only send Telegram push from existing dist/ artifacts")
    args = parser.parse_args()

    if args.notify_only:
        notify_from_dist()
    elif args.build_only:
        build(notify=False)
    else:
        build(notify=True)
