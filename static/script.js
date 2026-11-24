document.addEventListener('DOMContentLoaded', () => {
    const dashboard = document.getElementById('dashboard');
    const template = document.getElementById('market-card-template');
    const globalActionPill = document.getElementById('global-action-pill');
    const dataDateTag = document.getElementById('data-date-tag');

    const isStatic = window.location.hostname.includes('github.io') || window.location.protocol === 'file:';
    const DATA_API_URL = isStatic ? './api_data.json' : '/api/data';
    const ROLLOVER_API_URL = isStatic ? './api_rollover.json' : '/api/rollover';
    const AUTO_REFRESH_MS = 5 * 60 * 1000;

    const fmt = (n) => Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
    const sessionFormState = new Map();
    const sessionRolloverInputs = {};

    function getMarketEvents(market) {
        const owns = key => Object.prototype.hasOwnProperty.call(market, key);
        const isExcessTrim = event => Boolean(event && String(event.type || '').includes('+0.15x'));
        const isSignalChange = event => Boolean(event && !isExcessTrim(event)
            && Number.isFinite(Number(event.from_lev)) && Number.isFinite(Number(event.to_lev))
            && Number(event.from_lev).toFixed(2) !== Number(event.to_lev).toFixed(2));
        return {
            signal: owns('last_signal_change') ? market.last_signal_change
                : [market.last_action, market.last_trim].find(isSignalChange) || null,
            excess: owns('last_excess_trim') ? market.last_excess_trim
                : [market.last_trim, market.last_action].find(isExcessTrim) || null,
            legacySignal: !owns('last_signal_change'),
            legacyExcess: !owns('last_excess_trim')
        };
    }

    const marketLabel = ticker => ticker === 'QQQ' ? '美股' : ticker === '006208.TW' ? '台股' : ticker;
    const shortDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '') ? value.slice(5).replace('-', '/') : (value || '未知');

    function getFreshness(market) {
        const status = String(market.data_status || '');
        const fallback = Boolean(market.fallback_used || status.startsWith('fallback'));
        const datedLag = /^\d{4}-\d{2}-\d{2}$/.test(market.date || '')
            && /^\d{4}-\d{2}-\d{2}$/.test(market.expected_session || '') && market.date < market.expected_session;
        const stale = Boolean(market.is_stale || status === 'stale' || status === 'fallback_stale' || datedLag);
        const other = status && !['ok', 'stale', 'fallback', 'fallback_stale'].includes(status);
        const issues = [];
        if (datedLag) issues.push(`${marketLabel(market.ticker)}資料延遲：${shortDate(market.date)}，應${shortDate(market.expected_session)}`);
        else if (stale && !fallback) issues.push(`${marketLabel(market.ticker)}資料新鮮度未確認（資料${shortDate(market.date)}，應有日期待核）`);
        if (fallback) issues.push(`${marketLabel(market.ticker)}來源失敗，使用備援資料（截至${shortDate(market.date)}）`);
        if (other) issues.push(`${marketLabel(market.ticker)}資料來源狀態：${status}`);
        if ((stale || fallback || other) && market.ticker === 'QQQ' && market.secondary_source
            && market.secondary_source.provider === 'futu') {
            const backupStatus = market.secondary_source.status;
            const backupIssues = {
                authorization_required: '此網站尚未設定富途備援憑證',
                credential_configuration_invalid: '此網站的富途備援憑證設定不完整或衝突',
                private_key_invalid: '富途備援私鑰或解密密碼無法使用',
                authorization_failed: '富途備援授權被拒絕，請核對權限與有效期',
                provider_unavailable: '富途備援連線失敗',
                provider_http_error: '富途備援服務回應異常',
                provider_rejected: '富途備援請求未獲接受',
                rate_limited: '富途備援暫時限流',
                adjusted_price_basis_mismatch: '富途與 Yahoo 還原價口徑不符，已拒絕補價',
                raw_price_mismatch: '富途與 Yahoo 原始收盤價不符，已拒絕補價',
                missing_completed_session: '富途也尚未提供應有交易日的完整收盤資料'
            };
            if (Object.prototype.hasOwnProperty.call(backupIssues, backupStatus)) {
                issues.push(`${marketLabel(market.ticker)}：${backupIssues[backupStatus]}`);
            }
        }
        return { stale, fallback, blocked: Boolean(stale || fallback || other), issues };
    }

    function getStrategyState(market) {
        const state = market.strategy_state;
        return state && state.schema_version === 1 ? state : null;
    }

    function cooldownDirection(cd) {
        const before = cd.last_step_from, after = cd.last_step_to;
        if (before != null && after != null && Number.isFinite(Number(before)) && Number.isFinite(Number(after))) {
            if (Number(after) < Number(before)) return '減碼後階梯冷卻中';
            if (Number(after) > Number(before)) return '加碼後階梯冷卻中';
        }
        if (cd.last_step_event_kind === 'step_down') return '減碼後階梯冷卻中';
        if (cd.last_step_event_kind === 'step_up') return '加碼後階梯冷卻中';
        return '階梯冷卻中';
    }

    function cooldownText(state) {
        if (!state || !state.cooldown) return '冷卻資訊未提供（舊版資料）';
        const cd = state.cooldown;
        if (cd.active) return `${cooldownDirection(cd)} · 餘 ${cd.remaining_days} 個市場交易日`;
        if (cd.timer_active && !cd.applies_to_current_stage) return '防禦階段 · 階梯冷卻不適用';
        return '階梯冷卻已結束';
    }

    // 僅在受阻或資料日有觸發時附註，常態已就緒時不重複文字雜訊。
    function channelExtraState(state, action, includeD1) {
        if (!state || !state.cooldown) return '冷卻資訊未提供';
        const notes = [];
        const cd = state.cooldown;
        if (cd.active) {
            notes.push(`${cooldownDirection(cd)}，餘 ${cd.remaining_days} 個市場交易日`);
        }
        if (includeD1) {
            const d1Reasons = (state.blocked_actions || []).filter(item => item.action === action && item.reason === 'd1_filter')
                .map(item => item.label);
            notes.push(...d1Reasons);
        }
        if (state.triggers && state.triggers[action === 'step_up' ? 'breakout_up' : 'breakout_down']) {
            notes.push(action === 'step_up' ? '資料日已突破通道' : '資料日已跌破通道');
        }
        return [...new Set(notes)].join('；');
    }

    function bullReentryText(market, state) {
        const fastDays = market.fast_ma_len || (market.ticker === 'QQQ' ? 5 : 8);
        const slowDays = market.slow_ma_len || 220;
        let value = `重返多頭須收盤 ≥ ${market.sma_slow_val.toFixed(2)} 且 ${fastDays}MA ≥ ${slowDays}MA`;
        const reasons = state && state.priority_defense ? state.priority_defense.reasons || [] : [];
        const missing = [];
        if (reasons.includes('fast_below_annual')) missing.push(`${fastDays}MA仍低於${slowDays}MA`);
        if (reasons.includes('annual_grace_exhausted')) missing.push('收盤仍低於年線');
        if (missing.length) value += `（${missing.join('；')}）`;
        return value;
    }

    function isVReboundState(market, state) {
        return Boolean(
            market.v_rebound_active ||
            (state && (state.stage === 'v_rebound' || state.stage === 'v_rebound_handoff'))
        );
    }

    function vReboundLev(market, state) {
        if (Number.isFinite(market.v_rebound_lev)) return market.v_rebound_lev;
        if (state && state.v_rebound && Number.isFinite(state.v_rebound.lev)) return state.v_rebound.lev;
        return 2.0;
    }

    function resolvePrimaryStagePill(market, state) {
        const isVRebound = isVReboundState(market, state);
        const vLev = vReboundLev(market, state);
        if (!market.in_trend) {
            if (market.deep_bear_active || (state && state.stage === 'defense_deep')) {
                return { text: `🚨 C1 深熊減半 (${market.target_today.toFixed(1)}x)`, cls: 'bear' };
            }
            if (isVRebound) {
                return { text: `🚀 V轉先鋒 (${vLev.toFixed(1)}x)`, cls: 'bull' };
            }
            if (market.pre_bull_active || (state && state.stage === 'e3b')) {
                return { text: `⚡ E3b 季線總攻 (${market.target_today.toFixed(1)}x)`, cls: 'accent' };
            }
            if (state && state.stage === 'defense_recovered') {
                return { text: '常態防禦／季線復原', cls: 'accent' };
            }
            return { text: '常態防禦底倉', cls: 'bear' };
        }
        if (isVRebound) {
            return { text: `🚀 V轉先鋒 · 待交棒 (${vLev.toFixed(1)}x)`, cls: 'bull' };
        }
        if (state && state.grace && state.grace.active) {
            return { text: '多頭狀態', cls: 'warn' };
        }
        if (state && state.d1 && state.d1.blocked) {
            return { text: '多頭 · D1 凍結升檔', cls: 'warn' };
        }
        if (market.step_idx >= market.max_steps) {
            return { text: '多頭 · 滿載巡航', cls: 'bull' };
        }
        return { text: '多頭階梯', cls: 'bull' };
    }

    function renderEntryChecks(card, market, state, freshness) {
        const fastDays = market.fast_ma_len || (market.ticker === 'QQQ' ? 5 : 8);
        const slowDays = market.slow_ma_len || 220;
        const upDays = market.bo_up_len || (market.ticker === 'QQQ' ? 8 : 15);
        const d1Enabled = state && state.d1 && typeof state.d1.enabled === 'boolean'
            ? state.d1.enabled : market.ticker === 'QQQ';
        const inGrace = state && state.grace && state.grace.active;
        const trendStatus = market.in_trend ? (inGrace ? '維持中（年線容忍）' : '已恢復') : '尚未恢復';
        card.querySelector('.entry-trend-check').textContent = `① 多頭狀態：${trendStatus}。恢復須收盤 ≥ ${slowDays}MA，且 ${fastDays}MA ≥ ${slowDays}MA；恢復多頭不代表直接加碼。`;
        const breakoutBool = state && state.triggers && typeof state.triggers.breakout_up === 'boolean'
            ? state.triggers.breakout_up : null;
        const breakout = breakoutBool === null ? '判定未提供' : (breakoutBool ? '已突破' : '未突破');
        card.querySelector('.entry-breakout-check').textContent = `② 收盤突破：${breakout}。收盤 ${market.close.toFixed(2)} 須 > 前 ${upDays} 日最高收盤 ${market.high_bw.toFixed(2)}。`;
        let d1Text = '③ D1 濾網：本市場不啟用。';
        let d1Pass = true;
        if (d1Enabled) {
            const status = !market.in_trend ? '恢復多頭後仍須核對'
                : state && state.d1 ? (state.d1.blocked ? '未通過' : '已通過') : '判定未提供';
            d1Pass = Boolean(market.in_trend && state && state.d1 && !state.d1.blocked);
            const bear = Number.isFinite(market.sma_bear_val) ? market.sma_bear_val.toFixed(2) : '待更新';
            d1Text = `③ D1 濾網：${status}。${fastDays}MA ${market.sma_fast_val.toFixed(2)} 須 ≥ 60MA ${bear}。`;
        }
        card.querySelector('.entry-d1-check').textContent = d1Text;
        const cd = state && state.cooldown;
        let cooldown = '判定未提供';
        let cdPass = false;
        if (cd && Number.isFinite(cd.remaining_days)) {
            cdPass = cd.remaining_days <= 0;
            cooldown = cd.remaining_days > 0 ? `尚餘 ${cd.remaining_days} 個市場交易日` : '已完成';
            if (cd.step_event_on_data_bar) cooldown = `本期階梯動作已生效；新冷卻${cooldown}`;
        }
        card.querySelector('.entry-cooldown-check').textContent = `④ 階梯冷卻：${cooldown}。年線／深熊優先防禦不受階梯冷卻限制。`;
        card.querySelector('.entry-check-note').textContent = freshness.blocked
            ? '資料待更新，以上僅供截至資料日參考；暫不提供本期調倉指示。'
            : market.step_idx >= market.max_steps ? '已在最高階梯，不再升檔；目標仍依封頂與防禦規則計算。'
                : '階梯升檔須同時滿足以上必要條件，目標仍受封頂規則限制；條件可在同一收盤日一起成立。';

        const checkSummaryTitle = card.querySelector('.check-summary-title');
        if (checkSummaryTitle) {
            const activeChecks = [Boolean(market.in_trend), breakoutBool === true];
            if (d1Enabled) activeChecks.push(d1Pass);
            activeChecks.push(cdPass);
            const passed = activeChecks.filter(Boolean).length;
            const total = activeChecks.length;
            if (market.step_idx >= market.max_steps) {
                checkSummaryTitle.textContent = `🔍 階梯升檔四條件明細 · 已滿載 (${market.step_idx}/${market.max_steps})`;
            } else if (!market.in_trend) {
                checkSummaryTitle.textContent = `🔍 階梯升檔四條件明細 · 防禦期未啟用 (${passed}/${total})`;
            } else {
                checkSummaryTitle.textContent = `🔍 階梯升檔四條件明細 · ${passed}/${total} 條件成立`;
            }
        }
    }

    function buildCallout(market, state, freshness, changed, trimmed, excessEvt) {
        const trimLine = (market.trim_trigger_lev || (market.target_today + 0.15)).toFixed(2);
        const targetStr = `${market.target_today.toFixed(2)}x`;
        const fastDays = market.fast_ma_len || (market.ticker === 'QQQ' ? 5 : 8);
        const slowStr = market.sma_slow_val.toFixed(2);
        const bearStr = Number.isFinite(market.sma_bear_val) ? market.sma_bear_val.toFixed(2) : '--';
        const upDays = market.bo_up_len || (market.ticker === 'QQQ' ? 8 : 15);
        const dnDays = market.bo_dn_len || (market.ticker === 'QQQ' ? 8 : 25);

        let headline = '';
        let warnBox = false;
        if (freshness.blocked) {
            headline = `⚠️ 資料尚未更新（截至 ${market.date}）：僅供舊資料參考，暫停本期確定調倉指示。`;
            warnBox = true;
        } else if (changed) {
            headline = `⚡ 本期目標變更（${market.target_yesterday.toFixed(2)}x ➔ ${targetStr}）：請於執行窗口依下方試算器核對單一淨指令。`;
            warnBox = true;
        } else if (trimmed) {
            const fromStr = excessEvt ? excessEvt.from_lev.toFixed(2) : trimLine;
            headline = `✂️ 本期觸發模擬超標修剪（${fromStr}x ➔ ${targetStr}）：請核對實帳槓桿是否突破 ${trimLine}x。`;
            warnBox = true;
        } else {
            headline = `⏸️ 截至 ${market.date} 維持目標 ${targetStr}（無新訊號）：實帳槓桿 ≤ ${trimLine}x 即不動作。`;
        }

        const details = [];
        if (state) {
            const isVRebound = isVReboundState(market, state);
            const vState = state.v_rebound || null;
            const vLev = vReboundLev(market, state);
            const vTrig = Number.isFinite(market.v_rebound_trigger_price)
                ? market.v_rebound_trigger_price
                : (vState && Number.isFinite(vState.trigger_price) ? vState.trigger_price : null);
            const vLow = Number.isFinite(market.v_rebound_low_val)
                ? market.v_rebound_low_val
                : (vState && Number.isFinite(vState.low_val) ? vState.low_val : null);
            const vRally = Number.isFinite(market.v_rebound_rally_pct)
                ? market.v_rebound_rally_pct
                : (vState && Number.isFinite(vState.rally_pct) ? vState.rally_pct : null);

            if (state.cooldown && state.cooldown.step_event_on_data_bar) {
                details.push('資料日階梯動作已生效，冷卻自該動作重算');
            }
            if (!market.in_trend) {
                if (market.deep_bear_active) {
                    details.push(`🚨 ${state.label}：60MA 低於年線 97% 且 ${fastDays}MA < 60MA (${bearStr})，待快線站回季線即復原常態底倉 ${market.base_leverage.toFixed(1)}x`);
                } else if (isVRebound) {
                    details.push(`🚀 ${state.label}：保底 ${vLev.toFixed(1)}x，跌破 ${dnDays} 日低點 ${market.low_bw.toFixed(2)} 或落入 C1 深熊退回底倉；站穩年線後待主階梯 ≥ ${vLev.toFixed(1)}x 無縫交棒`);
                } else if (market.pre_bull_active) {
                    details.push(`⚡ ${state.label}：收盤與快線守穩季線 ≥ ${bearStr}；轉多頭仍須站上年線 ${slowStr}`);
                } else {
                    const preBoost = market.ticker === 'QQQ' ? '1.0x' : '1.2x';
                    details.push(`🛡️ ${state.label}：進入多頭後須突破 ${upDays} 日通道${state.d1 && state.d1.enabled ? '、D1 季線濾網' : ''}與冷卻；若季線下守穩 ${bearStr} 且收盤 > ${market.high_bw.toFixed(2)} 可先觸發 E3b (${preBoost})`);
                }
                if (!market.deep_bear_active && !isVRebound && vState && vState.enabled && Number.isFinite(vTrig)) {
                    const lowTxt = Number.isFinite(vLow) ? `20日低 ${vLow.toFixed(2)} ` : '20日低 ';
                    const rallyTxt = Number.isFinite(vRally) ? `，現 ${vRally >= 0 ? '+' : ''}${vRally.toFixed(1)}%` : '';
                    details.push(`🚀 V轉先鋒線 ≥ ${vTrig.toFixed(2)}（${lowTxt}+15%${rallyTxt}）`);
                }
            } else {
                if (state.grace && state.grace.active) {
                    details.push(`⚠️ 年線破線容忍 ${state.grace.days_elapsed}/${state.grace.days_allowed} 日（收盤低於年線 ${slowStr} 連續 ${state.grace.exit_after_consecutive_days} 日才退出，或快線死叉即退防禦）`);
                }
                if (state.cooldown && state.cooldown.active) {
                    details.push(`⏳ ${cooldownDirection(state.cooldown)}，餘 ${state.cooldown.remaining_days} 個市場交易日（期間加減碼凍結；破年線防禦與超標修剪除外）`);
                }
                if (state.d1 && state.d1.blocked) {
                    details.push(`🛡️ D1 僅凍結升檔（${fastDays}MA < 60MA ${bearStr}），減碼與防禦照常`);
                }
                if (state.extension && state.extension.capping_active) {
                    details.push(`🌡️ Extension 封頂 ${state.extension.cap.toFixed(1)}x（乖離 >16% 主動鎖利）`);
                }
                if (state.bull_acceleration && state.bull_acceleration.active) {
                    details.push(`🚀 牛初加速底倉 1.2x（含資料日餘 ${state.bull_acceleration.days_including_data_bar} 個市場交易日）`);
                }
                if (isVRebound) {
                    details.push(`🚀 V轉先鋒待交棒：保底 ${vLev.toFixed(1)}x 續抱，跌破 ${dnDays} 日低點 < ${market.low_bw.toFixed(2)} 止損；階梯突破 > ${market.high_bw.toFixed(2)} 升達 ≥ ${vLev.toFixed(1)}x 即無縫交棒`);
                }
                if ((state.flags || []).includes('full')) {
                    details.push(`🔒 階梯已滿載（${market.step_idx}/${market.max_steps}），跌破 ${dnDays} 日低點 < ${market.low_bw.toFixed(2)} 才降檔`);
                } else if (!isVRebound && !(state.grace && state.grace.active) && !(state.cooldown && state.cooldown.active) && !(state.d1 && state.d1.blocked) && !(state.extension && state.extension.capping_active)) {
                    const downHint = market.step_idx > 0 ? `跌破 < ${market.low_bw.toFixed(2)} 降一檔` : `守 220MA (${slowStr})`;
                    details.push(`階梯 ${market.step_idx}/${market.max_steps}：收盤 > ${market.high_bw.toFixed(2)} 升一檔、${downHint}`);
                }
            }
            if (!changed && !trimmed && !freshness.blocked) {
                details.push('實帳超過目標 +0.15x 仍須另核');
            }
        } else {
            details.push('舊版資料未提供獨立階段與冷卻計數；實帳超過目標 +0.15x 仍須另核');
        }

        return { headline, explanation: details.join('；') + '。', warnBox };
    }

    // 70/30 ±10% 半年跨市場配比檢核（永久記憶於 localStorage，預設折疊）
    function initRebalChecker() {
        const panel = document.getElementById('rebal-panel');
        const usInput = document.getElementById('rebal-us-usd');
        const twInput = document.getElementById('rebal-tw-twd');
        const fxInput = document.getElementById('rebal-fx');
        const resBox = document.getElementById('rebal-result');
        const sumDiv = document.getElementById('rebal-summary');
        const instDiv = document.getElementById('rebal-instruction');
        const pill = document.getElementById('rebal-status-pill');
        if (!panel || !usInput || !twInput || !fxInput || !resBox) return;

        const STORAGE_KEY = 'moat_cross_alloc_v1';
        try {
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
            if (saved) {
                if (saved.us !== undefined) usInput.value = saved.us;
                if (saved.tw !== undefined) twInput.value = saved.tw;
                if (saved.fx !== undefined) fxInput.value = saved.fx;
            }
        } catch (e) {}

        function evaluate() {
            const usVal = parseFloat(usInput.value);
            const twVal = parseFloat(twInput.value);
            const fxRaw = parseFloat(fxInput.value);
            const fx = (Number.isFinite(fxRaw) && fxRaw > 0) ? fxRaw : 32.5;
            try {
                localStorage.setItem(STORAGE_KEY, JSON.stringify({
                    us: usInput.value, tw: twInput.value, fx: fxInput.value
                }));
            } catch (e) {}

            if (!Number.isFinite(usVal) || usVal < 0 || !Number.isFinite(twVal) || twVal < 0 || (usVal === 0 && twVal === 0)) {
                resBox.classList.add('hidden');
                if (pill) pill.classList.add('hidden');
                return;
            }
            const usTwd = usVal * fx;
            const totalTwd = usTwd + twVal;
            const usPct = (usTwd / totalTwd) * 100;
            const twPct = 100 - usPct;
            const diffToCenterTwd = totalTwd * 0.70 - usTwd; // >0 代表須從台股匯往美股
            const inBand = usPct >= 60.0 && usPct <= 80.0;
            const emergencyTw = twPct > 50.0;

            resBox.classList.remove('hidden');
            if (pill) {
                pill.classList.remove('hidden', 'ok', 'danger');
                pill.classList.add(inBand ? 'ok' : 'danger');
                pill.textContent = `美 ${usPct.toFixed(1)}% / 台 ${twPct.toFixed(1)}%`;
            }

            sumDiv.innerHTML =
                `合計總資產: <strong>NT$ ${fmt(totalTwd)}</strong>（換算匯率 ${fx.toFixed(2)}） ｜ ` +
                `🇺🇸 美股: <strong>${usPct.toFixed(1)}%</strong> (NT$ ${fmt(usTwd)}) ｜ ` +
                `🇹🇼 台股: <strong>${twPct.toFixed(1)}%</strong> (NT$ ${fmt(twVal)})`;

            const lighterSide = usPct < 70 ? '🇺🇸 美股帳戶' : '🇹🇼 台股帳戶';
            if (inBand) {
                instDiv.innerHTML = `<div class="lev-check lev-ok">✅ <strong>【位於 70/30 ±10% 寬帶內（美 60%~80% / 台 20%~40%）】</strong>無須跨國調倉或電匯！每月新資金請優先存入偏輕側：<strong>${lighterSide}</strong>（目標中心為 美 $${fmt(totalTwd * 0.70 / fx)} / 台 NT$ ${fmt(totalTwd * 0.30)}）。</div>`;
            } else {
                const absMoveTwd = Math.abs(diffToCenterTwd);
                const absMoveUsd = absMoveTwd / fx;
                const moveDir = diffToCenterTwd > 0
                    ? `由 🇹🇼 台股移轉 <strong>NT$ ${fmt(absMoveTwd)}</strong>（約 <strong>USD $${fmt(absMoveUsd)}</strong>）至 🇺🇸 美股`
                    : `由 🇺🇸 美股移轉 <strong>USD $${fmt(absMoveUsd)}</strong>（約 <strong>NT$ ${fmt(absMoveTwd)}</strong>）至 🇹🇼 台股`;
                if (emergencyTw) {
                    instDiv.innerHTML = `<div class="lev-check lev-danger">🚨 <strong>【台股超過 50% · 觸發選配極端煞車線】</strong>當前比例偏離為 <strong>美 ${usPct.toFixed(1)}% / 台 ${twPct.toFixed(1)}%</strong>。若按中心拉回 70/30，須 ${moveDir}，避免單一市場過度集中。</div>`;
                } else {
                    instDiv.innerHTML = `<div class="lev-check lev-danger">⚠️ <strong>【已超出 70/30 ±10% 寬帶】</strong>當前比例為 <strong>美 ${usPct.toFixed(1)}% / 台 ${twPct.toFixed(1)}%</strong>。<br>• 若今日為 <strong>1 月初或 7 月初半年檢視日</strong>：請一次性 ${moveDir}，拉回 <strong>70/30 中心</strong>（重灌 10% 緩衝）。<br>• 若非檢視日（兩次檢查之間）：<strong>維持不動</strong>，靠每月新資金優先存往 <strong>${lighterSide}</strong> 自然收斂即可。</div>`;
                }
            }
        }

        [usInput, twInput, fxInput].forEach(el => el.addEventListener('input', evaluate));
        evaluate();
    }

    function fetchData(retryCount = 0) {
        fetch(DATA_API_URL + '?t=' + new Date().getTime())
            .then(res => {
                if (res.status === 202) {
                    setTimeout(() => fetchData(retryCount + 1), 5000);
                    return null;
                }
                return res.json();
            })
            .then(data => {
                if (!data) return;
                dashboard.innerHTML = '';
                dashboard.classList.remove('loading-state');

                const validMarkets = Array.isArray(data)
                    ? data.filter(m => m && typeof m === 'object' && typeof m.ticker === 'string' && typeof m.id === 'string' && typeof m.close === 'number' && !isNaN(m.close))
                    : [];

                if (validMarkets.length === 0) {
                    dashboard.innerHTML = `<div class="loading-box" style="color:#ef4444;">🚨 無法取得有效雙市場訊號（空陣列或來源中斷），系統已暫停自動指令，請檢查資料管線。</div>`;
                    dataDateTag.textContent = 'EOD: 資料缺失';
                    globalActionPill.textContent = '🚨 訊號資料異常 · 暫停自動指令';
                    globalActionPill.className = 'action-pill bear';
                    setTimeout(() => { fetchData(0); fetchRolloverData(); }, AUTO_REFRESH_MS);
                    return;
                }

                const seenTickers = new Set(validMarkets.map(m => m.ticker));
                const hasBothRequired = seenTickers.has('QQQ') && seenTickers.has('006208.TW') && validMarkets.length === 2;

                const changedNames = [];
                const trimNames = [];
                const dataIssues = ['QQQ', '006208.TW'].filter(ticker => !seenTickers.has(ticker))
                    .map(ticker => `${marketLabel(ticker)}市場資料缺失`);
                if (!hasBothRequired && dataIssues.length === 0) dataIssues.push('市場資料結構異常（重複或額外市場）');
                const marketDates = [];
                const activeRestrictions = [];

                validMarkets.forEach(market => {
                    if (market.date) {
                        const shortLabel = market.ticker === 'QQQ' ? 'US' : (market.ticker === '006208.TW' ? 'TW' : market.ticker);
                        marketDates.push({ label: shortLabel, ticker: market.ticker, date: market.date });
                    }
                    const freshness = getFreshness(market);
                    const state = getStrategyState(market);
                    if (state && (state.flags || []).some(flag => ['cooldown', 'grace', 'd1_filter', 'extension'].includes(flag))) {
                        const restriction = (state.cooldown && state.cooldown.active)
                            ? cooldownText(state)
                            : state.label;
                        activeRestrictions.push(`${marketLabel(market.ticker)}${restriction}`);
                    }
                    const targetChangedToday = !freshness.blocked && Number(market.target_today).toFixed(2)
                        !== Number(market.target_yesterday).toFixed(2);
                    if (targetChangedToday) changedNames.push(marketLabel(market.ticker));
                    const events = getMarketEvents(market);

                    const isTrimToday = Boolean(
                        !freshness.blocked && !targetChangedToday && events.excess &&
                        events.excess.date === market.date && events.excess.days_ago === 0
                    );
                    if (isTrimToday) trimNames.push(marketLabel(market.ticker));

                    dataIssues.push(...freshness.issues);

                    if (market.ticker === 'QQQ' && market.close) {
                        window.latestQqqClose = market.close;
                        const nqBaseSpan = document.querySelector('#rollover-radar .rollover-card[data-market="us"] .ro-nq-base');
                        if (nqBaseSpan) {
                            nqBaseSpan.textContent = `(估算基準 ~${Math.round(market.close * 41.15)}點，非即時NQ報價)`;
                        }
                    }

                    const clone = template.content.cloneNode(true);
                    const storageKey = `moat_calc_${market.id}`;

                    clone.querySelector('.market-name').textContent = market.name;
                    clone.querySelector('.ticker-code').textContent = market.ticker;
                    const cardDateBadge = clone.querySelector('.card-date-badge');
                    if (cardDateBadge) {
                        cardDateBadge.textContent = shortDate(market.date);
                    }
                    const eodPriceLabel = clone.querySelector('.eod-price-label');
                    if (eodPriceLabel && market.date) {
                        eodPriceLabel.textContent = `最新收盤 (${shortDate(market.date)})`;
                    }

                    // 單一主狀態徽章 + 僅於生效中之修飾標籤（不顯示「冷卻已結束」等靜止雜訊）
                    const primaryPill = resolvePrimaryStagePill(market, state);
                    const badge = clone.querySelector('.status-badge');
                    badge.textContent = primaryPill.text;
                    badge.classList.add(primaryPill.cls);

                    const extCapVal = market.ext_cap ? market.ext_cap.toFixed(1) : '1.8';
                    const extBadge = clone.querySelector('.ext-badge');
                    if (extBadge && market.ext_guard_active) {
                        extBadge.textContent = `🌡️ 過熱封頂 ${extCapVal}x`;
                        extBadge.classList.remove('hidden');
                    }
                    const accelBadge = clone.querySelector('.accel-badge');
                    if (accelBadge && market.bull_accel_active) {
                        const accelState = state && state.bull_acceleration;
                        const daysText = accelState && Number.isFinite(accelState.days_including_data_bar)
                            ? `含資料日餘 ${accelState.days_including_data_bar} 個市場交易日`
                            : `資料日後餘 ${market.bull_accel_days_left} 個市場交易日`;
                        accelBadge.textContent = `🚀 牛初加速 1.2x (${daysText})`;
                        accelBadge.classList.remove('hidden');
                    }
                    const staleBadge = clone.querySelector('.stale-badge');
                    if (staleBadge && freshness.blocked) {
                        staleBadge.textContent = `⚠️ ${freshness.issues.join('；')}`;
                        staleBadge.classList.remove('hidden');
                    }
                    const cdBadge = clone.querySelector('.cooldown-badge');
                    const showCdBadge = Boolean(!state || (state.cooldown && (state.cooldown.active || (state.cooldown.timer_active && !state.cooldown.applies_to_current_stage))));
                    if (cdBadge && showCdBadge) {
                        cdBadge.textContent = `⏳ ${cooldownText(state)}`;
                        cdBadge.classList.remove('hidden');
                    }
                    const graceBadge = clone.querySelector('.grace-badge');
                    if (graceBadge && state && state.grace && state.grace.active) {
                        graceBadge.textContent = `年線容忍 ${state.grace.days_elapsed}/${state.grace.days_allowed} 日`;
                        graceBadge.classList.remove('hidden');
                    }

                    // 本期訊號徽章
                    const deltaBadge = clone.querySelector('.delta-badge');
                    if (freshness.blocked) {
                        deltaBadge.textContent = `截至 ${market.date}：${market.target_today.toFixed(2)}x（待更新）`;
                        deltaBadge.classList.add('hold');
                    } else if (targetChangedToday) {
                        deltaBadge.textContent = `⚡ 目標變更 ${market.target_yesterday.toFixed(2)}x ➔ ${market.target_today.toFixed(2)}x`;
                        deltaBadge.classList.add('change');
                    } else if (isTrimToday) {
                        deltaBadge.textContent = `✂️ 本期模擬修剪（超標 ${events.excess.from_lev.toFixed(2)}x➔${market.target_today.toFixed(2)}x）`;
                        deltaBadge.classList.add('change');
                    } else {
                        deltaBadge.textContent = `維持 ${market.target_today.toFixed(2)}x`;
                        deltaBadge.classList.add('hold');
                    }

                    // 核心三指標
                    clone.querySelector('.price').textContent = market.close.toFixed(2);
                    const biasPct = ((market.close / market.sma_slow_val - 1) * 100).toFixed(1);
                    clone.querySelector('.ma-dist-text').textContent = `年線 ${market.sma_slow_val.toFixed(1)} (${biasPct >= 0 ? '+' : ''}${biasPct}%)`;

                    clone.querySelector('.target-lev').textContent = `${market.target_today.toFixed(2)}x`;
                    const trimLine = market.trim_trigger_lev || (market.target_today + 0.15);
                    clone.querySelector('.prev-and-trim').innerHTML = `修剪警戒 <strong style="color:#f59e0b;">>${trimLine.toFixed(2)}x</strong>`;

                    clone.querySelector('.steps').textContent = `${market.step_idx} / ${market.max_steps}`;
                    const track = clone.querySelector('.step-bar-track');
                    for (let s = 1; s <= market.max_steps; s++) {
                        const seg = document.createElement('div');
                        seg.className = 'step-seg' + (s <= market.step_idx ? ' active' : '');
                        track.appendChild(seg);
                    }

                    // 精簡行動摘要 Callout
                    const calloutData = buildCallout(market, state, freshness, targetChangedToday, isTrimToday, events.excess);
                    const calloutEl = clone.querySelector('.signal-callout');
                    if (calloutEl && calloutData.warnBox) calloutEl.classList.add('action-warn');
                    const headlineEl = clone.querySelector('.strategy-headline');
                    if (headlineEl) headlineEl.textContent = calloutData.headline;
                    clone.querySelector('.strategy-explanation').textContent = calloutData.explanation;

                    // 2x2 技術防線矩陣（修正防衛區下界語義，且不重複「冷卻已結束」）
                    const fastDays = market.fast_ma_len || (market.ticker === 'QQQ' ? 5 : 8);
                    const dnDays = market.bo_dn_len || (market.ticker === 'QQQ' ? 8 : 25);
                    const c1Thresh = (market.sma_slow_val * (market.deep_bear_ratio || 0.97)).toFixed(1);
                    const bearStr = Number.isFinite(market.sma_bear_val) ? market.sma_bear_val.toFixed(2) : '--';
                    const isVRebound = isVReboundState(market, state);
                    const vLev = vReboundLev(market, state);

                    const highBwElem = clone.querySelector('.high-bw-val');
                    const highBwLabel = clone.querySelector('.high-bw-label');
                    if (highBwLabel) highBwLabel.textContent = market.in_trend
                        ? '📈 階梯升檔門檻 (Close)' : '🧭 防禦轉多頭／E3b條件';
                    const isD1Frozen = state && state.d1 ? state.d1.blocked
                        : (market.ticker === 'QQQ') && market.in_trend && market.sma_bear_val && (market.sma_fast_val < market.sma_bear_val);
                    if (!market.in_trend) {
                        const reentry = bullReentryText(market, state);
                        if (market.pre_bull_active) {
                            highBwElem.textContent = `⚡ 季線總攻中（收盤與快線守季線 ≥ ${bearStr}）；${reentry}`;
                            highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                        } else if (market.sma_bear_val && market.close >= market.sma_bear_val && market.sma_fast_val >= market.sma_bear_val) {
                            highBwElem.textContent = `⚡ E3b 進場須收盤 > ${market.high_bw.toFixed(2)}；${reentry}`;
                            highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                        } else {
                            highBwElem.textContent = reentry;
                            highBwElem.className = 'tech-v font-mono warn-text high-bw-val';
                        }
                    } else if (market.ext_guard_active) {
                        highBwElem.textContent = `🌡️ 過熱封頂 ${extCapVal}x (>${market.high_bw.toFixed(1)})`;
                        highBwElem.className = 'tech-v font-mono warn-text high-bw-val';
                    } else if (isVRebound) {
                        highBwElem.textContent = `🚀 保底 ${vLev.toFixed(1)}x 待階梯升檔 > ${market.high_bw.toFixed(2)} 無縫交棒${isD1Frozen ? `（尚待 ${fastDays}MA ≥ 60MA ${bearStr}）` : ''}`;
                        highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                    } else if (market.step_idx >= market.max_steps) {
                        highBwElem.textContent = `🔒 已達 ${market.max_leverage.toFixed(1)}x 滿載上限`;
                        highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                    } else if (isD1Frozen) {
                        highBwElem.textContent = `收盤須 > ${market.high_bw.toFixed(2)} ｜ 🛡️ D1 凍結（待 ${fastDays}MA ≥ 60MA ${bearStr}）`;
                        highBwElem.className = 'tech-v font-mono warn-text high-bw-val';
                    } else {
                        highBwElem.textContent = `> ${market.high_bw.toFixed(2)}`;
                        highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                    }
                    const upExtra = channelExtraState(state, 'step_up', false);
                    if (upExtra) highBwElem.textContent += ` ｜ ${upExtra}`;
                    if (freshness.blocked) highBwElem.textContent += '（舊資料待更新）';

                    const lowBwElem = clone.querySelector('.low-bw-val');
                    const lowBwLabel = clone.querySelector('.low-bw-label');
                    if (lowBwLabel) lowBwLabel.textContent = isVRebound
                        ? '🚀 V轉先鋒止損門檻'
                        : ((market.in_trend && market.step_idx > 0) ? '📉 階梯減碼 / 退防門檻' : '🛡️ 當前底倉防守線');

                    if (!market.in_trend) {
                        if (market.deep_bear_active) {
                            lowBwElem.textContent = `🚨 已處深熊底倉 (${market.target_today.toFixed(1)}x) ｜ ${fastDays}MA ≥ 60MA (${bearStr}) 復原 ${market.base_leverage.toFixed(1)}x`;
                            lowBwElem.className = 'tech-v font-mono bear-text low-bw-val';
                        } else if (isVRebound) {
                            lowBwElem.textContent = `🚀 V轉先鋒止損：< ${market.low_bw.toFixed(2)}（${dnDays}日低）或落入 C1 深熊 ➔ 退回底倉`;
                            lowBwElem.className = 'tech-v font-mono warn-text low-bw-val';
                        } else if (market.pre_bull_active) {
                            lowBwElem.textContent = `守季線 60MA (${bearStr})（破線退回常態 ${market.base_leverage.toFixed(1)}x）`;
                            lowBwElem.className = 'tech-v font-mono warn-text low-bw-val';
                        } else {
                            lowBwElem.textContent = `C1 深熊線：60MA < ${c1Thresh} 且 ${fastDays}MA < 60MA ➔ 底倉減半`;
                            lowBwElem.className = 'tech-v font-mono warn-text low-bw-val';
                        }
                        if (!state) lowBwElem.textContent += ' ｜ 冷卻資訊未提供';
                    } else if (isVRebound) {
                        lowBwElem.textContent = `🚀 V轉先鋒止損：< ${market.low_bw.toFixed(2)}（${dnDays}日低）或落入 C1 深熊 ➔ 退回底倉`;
                        lowBwElem.className = 'tech-v font-mono warn-text low-bw-val';
                        const dnExtra = channelExtraState(state, 'step_down', false);
                        if (dnExtra) lowBwElem.textContent += ` ｜ ${dnExtra}`;
                    } else if (market.step_idx === 0) {
                        const exitDays = (state && state.grace && Number.isFinite(state.grace.exit_after_consecutive_days))
                            ? state.grace.exit_after_consecutive_days : 1;
                        const exitRule = exitDays > 1 ? `連 ${exitDays} 日` : '當日';
                        lowBwElem.textContent = `已處第 0 階 · 守 220MA (${market.sma_slow_val.toFixed(1)})（${exitRule}破線轉防禦）`;
                        lowBwElem.className = 'tech-v font-mono warn-text low-bw-val';
                        const dnExtra = channelExtraState(state, 'step_down', false);
                        if (dnExtra) lowBwElem.textContent += ` ｜ ${dnExtra}`;
                    } else {
                        lowBwElem.textContent = `< ${market.low_bw.toFixed(2)}（或破年線 ${market.sma_slow_val.toFixed(1)}）`;
                        lowBwElem.className = 'tech-v font-mono warn-text low-bw-val';
                        const dnExtra = channelExtraState(state, 'step_down', false);
                        if (dnExtra) lowBwElem.textContent += ` ｜ ${dnExtra}`;
                    }
                    if (freshness.blocked) lowBwElem.textContent += '（舊資料待更新）';

                    // Box 3: 關鍵均線整併（相容舊 test selectors）
                    const maSumEl = clone.querySelector('.ma-summary-val');
                    if (maSumEl) {
                        maSumEl.textContent = `${fastDays}MA ${market.sma_fast_val.toFixed(1)} / 60MA ${market.sma_bear_val ? market.sma_bear_val.toFixed(1) : '--'} / 年 ${market.sma_slow_val.toFixed(1)}（C1 < ${c1Thresh}）`;
                    }

                    // Box 4: 策略事件紀錄（精簡為兩行）
                    const fmtEvent = (evt, legacy) => {
                        if (!evt) return legacy ? '舊版未分列；待更新' : '尚無獨立紀錄';
                        const dTag = evt.days_ago === 0 ? '當期' : `${evt.days_ago}d前`;
                        return `${shortDate(evt.date)} (${dTag}) ${evt.from_lev.toFixed(2)}x➔${evt.to_lev.toFixed(2)}x [${evt.type}]`;
                    };
                    const lastActElem = clone.querySelector('.last-action-val');
                    if (lastActElem) lastActElem.textContent = fmtEvent(events.signal, events.legacySignal);
                    const lastTrimElem = clone.querySelector('.last-trim-val');
                    if (lastTrimElem) lastTrimElem.textContent = fmtEvent(events.excess, events.legacyExcess);

                    renderEntryChecks(clone, market, state, freshness);

                    // 實戰部位與 DCA 試算器（修正 position=0 空手試算與降檔+DCA 自相矛盾文案）
                    const equityInput = clone.querySelector('.equity-input');
                    const positionInput = clone.querySelector('.position-input');
                    const dcaInput = clone.querySelector('.dca-input');
                    const resultBox = clone.querySelector('.action-result');
                    const summaryDiv = clone.querySelector('.calc-summary');
                    const instructionDiv = clone.querySelector('.action-instruction');
                    const savedPill = clone.querySelector('.saved-lev-pill');
                    const calcDrawer = clone.querySelector('.calc-drawer');
                    const savedMetaNote = clone.querySelector('.saved-meta-note');
                    const freshnessNote = clone.querySelector('.calc-freshness-note');
                    if (freshnessNote && freshness.blocked) {
                        freshnessNote.textContent = `${freshness.issues.join('；')}。保留輸入，暫停確定調倉試算，待最新資料更新。`;
                        freshnessNote.classList.remove('hidden');
                    }

                    function syncSessionFormState() {
                        sessionFormState.set(market.id, {
                            equity: equityInput.value,
                            position: positionInput.value,
                            dca: dcaInput ? dcaInput.value : '',
                            open: Boolean(calcDrawer && calcDrawer.open)
                        });
                    }

                    function saveState(isConfirmedReal) {
                        if (!isConfirmedReal) return;
                        try {
                            const nowIso = new Date().toISOString().slice(0, 16).replace('T', ' ');
                            localStorage.setItem(storageKey, JSON.stringify({
                                equity: equityInput.value,
                                position: positionInput.value,
                                savedAt: nowIso,
                                marketDate: market.date || ''
                            }));
                            if (savedMetaNote) {
                                savedMetaNote.textContent = `💾 實帳記憶: ${nowIso} (訊號日 ${market.date || '--'})`;
                            }
                        } catch (e) {}
                    }

                    function renderConditionalPreview() {
                        const targetToday = market.target_today;
                        const eqValStr = equityInput.value.trim();
                        const posValStr = positionInput.value.trim();
                        const eqRaw = parseFloat(eqValStr);
                        const posRaw = parseFloat(posValStr);
                        const dcaRaw = dcaInput ? parseFloat(dcaInput.value) : 0;
                        const hasEq = eqValStr !== '' && Number.isFinite(eqRaw) && eqRaw > 0;
                        const hasPos = posValStr !== '' && Number.isFinite(posRaw) && posRaw >= 0;
                        const dcaVal = (Number.isFinite(dcaRaw) && dcaRaw > 0) ? dcaRaw : 0;

                        if (!hasEq && !hasPos && dcaVal <= 0) {
                            resultBox.classList.add('hidden');
                            savedPill.classList.add('hidden');
                            return;
                        }

                        resultBox.classList.remove('hidden');
                        savedPill.classList.remove('hidden', 'ok');
                        savedPill.classList.add('danger');
                        savedPill.textContent = '⚠️ 缺實帳欄位（條件試算）';

                        if (hasEq && !hasPos) {
                            const eqAfter = eqRaw + dcaVal;
                            const condTargetPos = Math.round(eqAfter * targetToday);
                            const marginAcct = Math.round(eqAfter * 2 / 3);
                            const idleCash = Math.round(eqAfter * 1 / 3);
                            summaryDiv.innerHTML =
                                `條件入金後淨值: <strong>$${fmt(eqAfter)}</strong> ｜ 條件目標總曝險 (${targetToday.toFixed(2)}x): <strong>$${fmt(condTargetPos)}</strong> ｜ ` +
                                `2/3 保證金池: <strong>$${fmt(marginAcct)}</strong> ｜ 1/3 防禦現金: <strong>$${fmt(idleCash)}</strong>`;
                            instructionDiv.innerHTML =
                                `<div class="lev-check lev-ok">📐 <strong>【條件試算預覽（尚未輸入目前實帳總曝險）】</strong>若以淨值 <strong>$${fmt(eqAfter)}</strong> 按今日目標 <strong>${targetToday.toFixed(2)}x</strong> 試算，條件目標總曝險為 <strong>$${fmt(condTargetPos)}</strong>。因缺少目前實帳總曝險，請補填（空手請填 0）以計算調倉差額。</div>`;
                        } else if (!hasEq && hasPos) {
                            const condReqEq = Math.round(posRaw / targetToday);
                            summaryDiv.innerHTML =
                                `已填目前總曝險: <strong>$${fmt(posRaw)}</strong>${dcaVal > 0 ? ` ｜ 擬投入新資金: <strong>$${fmt(dcaVal)}</strong>` : ''} ｜ ` +
                                `對應 ${targetToday.toFixed(2)}x 所需基準淨值: <strong>$${fmt(condReqEq)}</strong>`;
                            instructionDiv.innerHTML =
                                `<div class="lev-check lev-ok">📐 <strong>【條件試算預覽（尚未輸入本市場分配淨值 NAV）】</strong>缺少本市場真實淨值 (NAV)，系統嚴禁反推實質槓桿或確定買賣金額；請補填淨值以整合計算。</div>`;
                        } else {
                            const dcaExposure = Math.round(dcaVal * targetToday);
                            summaryDiv.innerHTML = `目標槓桿: <strong>${targetToday.toFixed(2)}x</strong> ｜ 本月分配新資金: <strong>$${fmt(dcaVal)}</strong>`;
                            instructionDiv.innerHTML = `<div class="lev-check lev-ok">💡 <strong>【僅輸入新資金條件試算】</strong>單獨新資金 <strong>$${fmt(dcaVal)}</strong> × <strong>${targetToday.toFixed(2)}x</strong> = 條件增量曝險 <strong>$${fmt(dcaExposure)}</strong>（未含舊倉修剪檢定）。</div>`;
                        }
                    }

                    function calculateAction(equity, position) {
                        const targetToday = market.target_today;
                        const prevTarget = market.target_yesterday !== undefined ? market.target_yesterday : targetToday;
                        const rawDca = dcaInput ? parseFloat(dcaInput.value) : 0;
                        const dcaVal = (Number.isFinite(rawDca) && rawDca > 0) ? rawDca : 0;

                        const equityAfterDca = equity + dcaVal;
                        const actualLevBeforeDca = position / equity;
                        const levAfterCash = position / equityAfterDca;

                        const isStepUp = (targetToday - prevTarget) > 0.005;
                        const isStepDn = (prevTarget - targetToday) > 0.005;
                        const sigReb = isStepUp || (isStepDn && levAfterCash > targetToday + 1e-9);

                        let plannedN;
                        if (sigReb) {
                            plannedN = equityAfterDca * targetToday;
                        } else if (dcaVal > 0) {
                            plannedN = position + dcaVal * targetToday;
                        } else {
                            plannedN = position;
                        }

                        const rawPlannedL = plannedN / equityAfterDca;
                        let plannedL = rawPlannedL;
                        let trimTriggered = false;
                        if (!sigReb && plannedL > trimLine + 1e-9) {
                            plannedN = equityAfterDca * targetToday;
                            plannedL = targetToday;
                            trimTriggered = true;
                        }

                        const netOrder = Math.round(plannedN - position);
                        const finalTargetPos = Math.round(plannedN);
                        const estFee = Math.round(Math.abs(netOrder) * 0.0001);
                        const equityAfterFee = Math.max(1, equityAfterDca - estFee);
                        const postFeeLev = plannedN / equityAfterFee;

                        const marginAcct = Math.round(equityAfterDca * 2 / 3);
                        const idleCash = Math.round(equityAfterDca * 1 / 3);

                        resultBox.classList.remove('hidden');
                        savedPill.classList.remove('hidden', 'danger', 'ok');
                        savedPill.textContent = `實質 ${actualLevBeforeDca.toFixed(2)}x`;
                        savedPill.classList.add((actualLevBeforeDca > trimLine || trimTriggered || (sigReb && Math.abs(netOrder) > 0)) ? 'danger' : 'ok');

                        if (actualLevBeforeDca > trimLine || trimTriggered || (isStepUp && actualLevBeforeDca > targetToday + 0.01)) {
                            calcDrawer.open = true;
                        }

                        summaryDiv.innerHTML =
                            `入金後淨值: <strong>$${fmt(equityAfterDca)}</strong> ｜ 目標總曝險: <strong>$${fmt(finalTargetPos)}</strong> ｜ ` +
                            `2/3 保證金池: <strong>$${fmt(marginAcct)}</strong> ｜ 1/3 防禦現金: <strong>$${fmt(idleCash)}</strong>`;

                        let levCheck = '';
                        const orderTag = netOrder > 0
                            ? `請淨加買 <strong>$${fmt(netOrder)}</strong> 曝險`
                            : (netOrder < 0 ? `請淨減持 <strong>$${fmt(Math.abs(netOrder))}</strong> 曝險` : `無須下單 (<strong>$0</strong>)`);

                        if (isStepUp) {
                            if (netOrder > 0) {
                                levCheck = `<div class="lev-check lev-danger">📈 <strong>【系統升檔加碼 ${prevTarget.toFixed(2)}x ➔ ${targetToday.toFixed(2)}x】</strong>入金後原始槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 低於新目標。<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險調至 <strong>$${fmt(finalTargetPos)}</strong>，執行後 <strong>${plannedL.toFixed(2)}x</strong>，含 1bp 摩擦約 ${postFeeLev.toFixed(2)}x）。</div>`;
                            } else if (netOrder < 0) {
                                levCheck = `<div class="lev-check lev-danger">⚠️ <strong>【系統升檔 ${prevTarget.toFixed(2)}x ➔ ${targetToday.toFixed(2)}x · 實帳已超標須下修】</strong>目前實際槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 已高於新目標！<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險壓回 <strong>$${fmt(finalTargetPos)}</strong>，執行後 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            } else {
                                levCheck = `<div class="lev-check lev-ok">✅ <strong>【系統升檔 ${prevTarget.toFixed(2)}x ➔ ${targetToday.toFixed(2)}x】</strong>目前實際槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 已對齊目標，${orderTag}。</div>`;
                            }
                        } else if (isStepDn) {
                            if (sigReb && netOrder < 0) {
                                levCheck = `<div class="lev-check lev-danger">📉 <strong>【系統降檔減碼 ${prevTarget.toFixed(2)}x ➔ ${targetToday.toFixed(2)}x】</strong>入金後槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 高於新目標（換檔日不適用 +0.15x 緩衝）！<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險壓回 <strong>$${fmt(finalTargetPos)}</strong>，執行後 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            } else if (dcaVal > 0 && netOrder > 0) {
                                levCheck = `<div class="lev-check lev-ok">💡 <strong>【系統降檔 · 舊倉不補買，新資金按 ${targetToday.toFixed(2)}x 投入】</strong>目標雖由 ${prevTarget.toFixed(2)}x 降至 <strong>${targetToday.toFixed(2)}x</strong>，舊倉實質槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 低於新目標不逆勢補買，僅將本次新資金 <strong>$${fmt(dcaVal)}</strong> 按 <strong>${targetToday.toFixed(2)}x</strong> 投入。<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險 <strong>$${fmt(finalTargetPos)}</strong>，執行後 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            } else {
                                levCheck = `<div class="lev-check lev-ok">✅ <strong>【系統降檔 · 自然衰退免動作 (No-Buy-Down)】</strong>目標雖由 ${prevTarget.toFixed(2)}x 降至 <strong>${targetToday.toFixed(2)}x</strong>，實質槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 已低於新目標。<br>🛡️ <strong>單一淨指令：${orderTag}</strong>（嚴禁逆勢加買補倉，維持總曝險 <strong>$${fmt(finalTargetPos)}</strong>，槓桿 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            }
                        } else {
                            if (trimTriggered) {
                                levCheck = `<div class="lev-check lev-danger">🚨 <strong>【超標修剪與 DCA 整合淨指令】</strong>入金前槓桿 <strong>${actualLevBeforeDca.toFixed(2)}x</strong>${dcaVal > 0 ? `（含新資金 $${fmt(dcaVal)} 後將達 ${rawPlannedL.toFixed(2)}x）` : ''}，突破修剪線 <strong>${trimLine.toFixed(2)}x</strong>！<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險壓回 <strong>$${fmt(finalTargetPos)}</strong>，執行後 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            } else if (dcaVal > 0) {
                                levCheck = `<div class="lev-check lev-ok">💡 <strong>【DCA 新資金單向投入整合淨指令】</strong>入金前 <strong>${actualLevBeforeDca.toFixed(2)}x</strong>，注入 <strong>$${fmt(dcaVal)}</strong> 稀釋至 <strong>${levAfterCash.toFixed(2)}x</strong>，僅新資金按 <strong>${targetToday.toFixed(2)}x</strong> 建倉（執行後 <strong>${plannedL.toFixed(2)}x</strong> ≤ ${trimLine.toFixed(2)}x，免修剪）。<br>✅ <strong>單一淨指令：${orderTag}</strong>（目標總曝險 <strong>$${fmt(finalTargetPos)}</strong>）。</div>`;
                            } else if (actualLevBeforeDca < targetToday) {
                                levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLevBeforeDca.toFixed(2)}x</strong>（低於目標 ${targetToday.toFixed(2)}x）— ${position === 0 ? '目前空手' : '自然衰退中'}，<strong>單一淨指令：${orderTag}</strong>（不追高補倉）。</div>`;
                            } else {
                                levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLevBeforeDca.toFixed(2)}x</strong>（介於 ${targetToday.toFixed(2)}x ～ ${trimLine.toFixed(2)}x 緩衝帶內）— <strong>單一淨指令：${orderTag}</strong>。</div>`;
                            }
                        }
                        instructionDiv.innerHTML = levCheck;
                    }

                    function evaluateInputs(shouldPersistReal) {
                        syncSessionFormState();
                        if (freshness.blocked) {
                            if (shouldPersistReal) saveState(true);
                            resultBox.classList.remove('hidden');
                            savedPill.classList.remove('hidden', 'ok');
                            savedPill.classList.add('danger');
                            savedPill.textContent = '資料待更新（不提供確定淨單）';
                            summaryDiv.textContent = `僅供截至 ${market.date} 的舊資料參考：目標 ${market.target_today.toFixed(2)}x`;
                            instructionDiv.textContent = '已保留本頁輸入；資料新鮮度未通過，請待最新資料更新後重新核對實帳，暫不提供買賣指令。';
                            return;
                        }
                        const eqVal = equityInput.value.trim();
                        const posVal = positionInput.value.trim();
                        const equity = parseFloat(eqVal);
                        const position = parseFloat(posVal);
                        const hasBothReal = (eqVal !== '' && Number.isFinite(equity) && equity > 0
                            && posVal !== '' && Number.isFinite(position) && position >= 0);
                        if (hasBothReal) {
                            if (shouldPersistReal) saveState(true);
                            calculateAction(equity, position);
                        } else {
                            renderConditionalPreview();
                        }
                    }

                    equityInput.addEventListener('input', () => evaluateInputs(true));
                    positionInput.addEventListener('input', () => evaluateInputs(true));
                    dcaInput.addEventListener('input', () => evaluateInputs(false));
                    if (calcDrawer) calcDrawer.addEventListener('toggle', () => syncSessionFormState());

                    const sessionSaved = sessionFormState.get(market.id);
                    if (sessionSaved && (sessionSaved.equity !== '' || sessionSaved.position !== '' || sessionSaved.dca !== '')) {
                        equityInput.value = sessionSaved.equity || '';
                        positionInput.value = sessionSaved.position || '';
                        if (dcaInput) dcaInput.value = sessionSaved.dca || '';
                        if (calcDrawer && sessionSaved.open) calcDrawer.open = true;
                        try {
                            const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
                            if (saved && savedMetaNote && saved.savedAt) {
                                const staleSaved = saved.marketDate && market.date && saved.marketDate !== market.date;
                                savedMetaNote.textContent = `💾 實帳記憶: ${saved.savedAt}${staleSaved ? ' ⚠️ (跨日舊部位，請更新收盤淨值)' : ''}`;
                            }
                        } catch (e) {}
                        evaluateInputs(false);
                    } else {
                        try {
                            const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
                            if (saved && saved.equity !== undefined && saved.equity !== '' && saved.position !== undefined && saved.position !== '') {
                                equityInput.value = saved.equity;
                                positionInput.value = saved.position;
                                if (savedMetaNote) {
                                    const staleSaved = saved.marketDate && market.date && saved.marketDate !== market.date;
                                    savedMetaNote.textContent = saved.savedAt
                                        ? `💾 實帳記憶: ${saved.savedAt}${staleSaved ? ' ⚠️ (跨日舊部位，請更新收盤淨值)' : ''}`
                                        : '💾 已載入上次記憶部位（請確認是否為最新收盤實帳）';
                                }
                                evaluateInputs(false);
                            }
                        } catch (e) {}
                    }

                    dashboard.appendChild(clone);
                });

                // 頂部精簡日期顯示（不再重複同一日期三次）
                if (marketDates.length >= 2) {
                    const allSameDate = marketDates.every(m => m.date === marketDates[0].date);
                    dataDateTag.textContent = allSameDate
                        ? `EOD ${marketDates[0].date}`
                        : `${marketDates.map(m => `${m.label} ${shortDate(m.date)}`).join(' · ')}（各市場交易日分列）`;
                } else if (marketDates.length === 1) {
                    dataDateTag.textContent = `EOD ${marketDates[0].label} ${marketDates[0].date} ⚠️ 單一市場缺失`;
                }

                // 頂部精簡全域動作膠囊（去重，無操作時僅精簡一句）
                if (dataIssues.length) {
                    globalActionPill.textContent = `⚠️ ${dataIssues.join(' ｜ ')} · 暫停調倉指示`;
                    globalActionPill.className = 'action-pill warn';
                } else if (changedNames.length) {
                    globalActionPill.textContent = `⚡ ${changedNames.join(' / ')} 目標槓桿變更 · 請展開試算器核對`;
                    globalActionPill.className = 'action-pill warn';
                } else if (trimNames.length) {
                    globalActionPill.textContent = `✂️ ${trimNames.join(' / ')} 模擬超標修剪 · 請核對實帳槓桿`;
                    if (activeRestrictions.length) globalActionPill.textContent += ` · ${activeRestrictions.join(' ｜ ')}`;
                    globalActionPill.className = 'action-pill warn';
                } else if (activeRestrictions.length) {
                    globalActionPill.textContent = `⏸️ 本期目標維持 · ${activeRestrictions.join(' ｜ ')}`;
                    globalActionPill.className = 'action-pill neutral';
                } else {
                    globalActionPill.textContent = '✅ 雙市場維持目標 · 實帳超過目標+0.15x才需調整';
                    globalActionPill.className = 'action-pill bull';
                }

                setTimeout(() => { fetchData(0); fetchRolloverData(); }, AUTO_REFRESH_MS);
            })
            .catch(err => {
                dashboard.innerHTML = `<div class="loading-box" style="color:#ef4444;">連線失敗：${err.message}</div>`;
                globalActionPill.textContent = '🚨 訊號連線失敗';
                globalActionPill.className = 'action-pill bear';
                setTimeout(() => { fetchData(0); fetchRolloverData(); }, AUTO_REFRESH_MS);
            });
    }

    function fetchRolloverData() {
        const container = document.getElementById('rollover-cards');
        if (!container) return;

        fetch(ROLLOVER_API_URL + '?t=' + new Date().getTime())
            .then(res => res.json())
            .then(data => {
                container.innerHTML = '';
                const badgeClsMap = { bull: 'bull', warning: 'warn', bear: 'bear', neutral: 'neutral' };
                ['tw', 'us'].forEach(key => {
                    const item = data[key];
                    if (!item) return;

                    const card = document.createElement('div');
                    card.className = `rollover-card ${item.status_class}`;
                    card.dataset.market = key;
                    const badgeCls = badgeClsMap[item.status_class] || 'neutral';
                    const inputPlaceholder = key === 'tw' ? '例: -20 或 10' : '例: 240';
                    const spreadTitle = key === 'tw' ? '⚡ 跨月價差試算' : '⚡ 跨季價差試算 <span class="ro-nq-base font-mono"></span>';

                    card.innerHTML = `
                        <div class="ro-header-compact">
                            <div class="ro-title-group">
                                <span class="ro-market-name">${item.market}</span>
                                <span class="ro-contract-tag font-mono">${item.contract_info}</span>
                            </div>
                            <span class="status-badge ${badgeCls}">${item.badge}</span>
                        </div>
                        <div class="ro-summary-bar">
                            <span class="ro-countdown-chip font-mono ${item.days_left <= 3 ? 'urgent' : ''}">
                                結算倒數 ${item.days_left}d (${item.settlement_date})
                            </span>
                            <span>${item.action}</span>
                        </div>
                        <div class="ro-input-row">
                            <label class="ro-input-label">${spreadTitle}</label>
                            <div class="ro-input-wrapper">
                                <input type="number" class="ro-spread-input font-mono" placeholder="${inputPlaceholder}" step="any">
                                <span class="ro-input-unit">點</span>
                            </div>
                        </div>
                        <div class="ro-verdict-compact hidden"></div>
                    `;

                    const input = card.querySelector('.ro-spread-input');
                    const verdict = card.querySelector('.ro-verdict-compact');
                    const isDeadline = (item.phase === 'deadline') || (key === 'tw' ? item.days_left <= 0 : item.days_left <= 1);

                    if (key === 'tw') {
                        input.addEventListener('input', () => {
                            const val = input.value.trim();
                            sessionRolloverInputs[key] = val;
                            if (val === '') { verdict.classList.add('hidden'); return; }
                            const spread = parseFloat(val);
                            if (isNaN(spread)) { verdict.classList.add('hidden'); return; }
                            verdict.classList.remove('hidden');
                            if (isDeadline) {
                                verdict.className = 'ro-verdict-compact bear';
                                verdict.innerHTML = `🚨 <strong>【今日 13:30 結算硬期限 · 立即換約】</strong>目前點差 ${spread >= 0 ? '+' : ''}${spread} 點，今日最後結算日嚴禁等待！請務必於 13:30 前完成跨月價差換約！`;
                            } else if (spread <= 0) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🔥 <strong>逆價差送分題 (${spread} 點)</strong>：遠月折價，立即掛跨月價差單換約！`;
                            } else if (spread <= 15) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>價差極窄 (+${spread} 點)</strong>：摩擦極低，建議直接掛價差單換約。`;
                            } else if (spread <= 25) {
                                verdict.className = 'ro-verdict-compact neutral';
                                verdict.innerHTML = `🟡 <strong>常態價差 (+${spread} 點)</strong>：可等盤中收斂或結算前 1~2 天再換。`;
                            } else {
                                verdict.className = 'ro-verdict-compact bear';
                                verdict.innerHTML = `🔴 <strong>價差偏大 (+${spread} 點)</strong>：遠月溢價偏高，非結算日可先觀察。`;
                            }
                        });
                    } else {
                        const nqBaseSpan = card.querySelector('.ro-nq-base');
                        const getNqImplied = () => (window.latestQqqClose || 700) * 41.15;
                        if (nqBaseSpan) nqBaseSpan.textContent = `(估算基準 ~${Math.round(getNqImplied())}點，非即時NQ報價)`;

                        input.addEventListener('input', () => {
                            const val = input.value.trim();
                            sessionRolloverInputs[key] = val;
                            if (val === '') { verdict.classList.add('hidden'); return; }
                            const spread = parseFloat(val);
                            if (isNaN(spread)) { verdict.classList.add('hidden'); return; }
                            const annRate = (spread / getNqImplied()) * (365 / 91) * 100;
                            verdict.classList.remove('hidden');
                            if (isDeadline) {
                                verdict.className = 'ro-verdict-compact bear';
                                verdict.innerHTML = `🚨 <strong>【結算硬期限 · 立即換約】</strong>估算年化隱含成本 ${annRate.toFixed(2)}%，NQ/MNQ 於結算日 09:30 ET 開盤即停止交易結算！請立即以 Calendar Spread 完成換約！`;
                            } else if (annRate <= 0) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🔥 <strong>罕見逆價差 (估算年化 ${annRate.toFixed(2)}%)</strong>：立即掛單換約！`;
                            } else if (annRate <= 4.2) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>超值低價 (估算年化 ${annRate.toFixed(2)}%)</strong>：低於短債利率，掛 Mid-Price 換約！`;
                            } else if (annRate <= 4.8) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>合理定價 (估算年化 ${annRate.toFixed(2)}%)</strong>：符合常態利率，於轉倉觀察窗從容換約。`;
                            } else {
                                verdict.className = 'ro-verdict-compact bear';
                                verdict.innerHTML = `🔴 <strong>溢價偏貴 (估算年化 ${annRate.toFixed(2)}%)</strong>：非結算期限可留意價差收斂再換。`;
                            }
                        });
                    }
                    if (sessionRolloverInputs[key] !== undefined && sessionRolloverInputs[key] !== '') {
                        input.value = sessionRolloverInputs[key];
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    container.appendChild(card);
                });
            });
    }

    initRebalChecker();
    fetchData();
    fetchRolloverData();
});
