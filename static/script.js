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

                // F03/F12: 空陣列、非陣列或全 null 元素時明確顯示紅燈錯誤，嚴禁顯示綠燈「今日目標槓桿不變」
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

                let anyLevChanged = false;
                let anyTrimToday = false;
                let anyStaleOrFallback = !hasBothRequired;
                const marketDates = [];

                validMarkets.forEach(market => {
                    if (market.date) {
                        const shortLabel = market.ticker === 'QQQ' ? 'US' : (market.ticker === '006208.TW' ? 'TW' : market.ticker);
                        marketDates.push({ label: shortLabel, ticker: market.ticker, date: market.date });
                    }
                    if (market.target_today !== market.target_yesterday) anyLevChanged = true;

                    const isTrimToday = Boolean(
                        market.last_trim &&
                        market.last_trim.days_ago === 0 &&
                        String(market.last_trim.type || '').includes('+0.15x')
                    );
                    if (isTrimToday) anyTrimToday = true;

                    if (market.is_stale || market.fallback_used || (market.data_status && market.data_status !== 'ok')) {
                        anyStaleOrFallback = true;
                    }

                    if (market.ticker === 'QQQ' && market.close) {
                        window.latestQqqClose = market.close;
                        const nqBaseSpan = document.querySelector('.ro-nq-base');
                        if (nqBaseSpan) {
                            nqBaseSpan.textContent = `(估算基準 ~${Math.round(market.close * 41.15)}點，非即時NQ報價)`;
                        }
                    }

                    const clone = template.content.cloneNode(true);
                    const storageKey = `moat_calc_${market.id}`;

                    // 標題、卡片專屬日期與狀態標籤 (F12)
                    clone.querySelector('.market-name').textContent = market.name;
                    clone.querySelector('.ticker-code').textContent = market.ticker;
                    const cardDateBadge = clone.querySelector('.card-date-badge');
                    if (cardDateBadge) {
                        cardDateBadge.textContent = `EOD: ${market.date || '--'}`;
                    }
                    const eodPriceLabel = clone.querySelector('.eod-price-label');
                    if (eodPriceLabel && market.date) {
                        eodPriceLabel.textContent = `最新收盤 (${market.date})`;
                    }

                    const badge = clone.querySelector('.status-badge');
                    if (market.in_trend) {
                        badge.textContent = '🟢 220MA 多頭安全區';
                        badge.classList.add('bull');
                    } else {
                        badge.textContent = '🔴 破年線防禦區';
                        badge.classList.add('bear');
                    }

                    // 附加狀態標籤
                    const extCapVal = market.ext_cap ? market.ext_cap.toFixed(1) : '1.8';
                    const extBadge = clone.querySelector('.ext-badge');
                    if (market.ext_guard_active) {
                        extBadge.textContent = `🌡️ 乖離過熱鎖 ${extCapVal}x`;
                        extBadge.classList.remove('hidden');
                    }
                    const accelBadge = clone.querySelector('.accel-badge');
                    if (market.bull_accel_active) {
                        accelBadge.textContent = `🚀 牛初加速 1.2x (餘 ${market.bull_accel_days_left}d)`;
                        accelBadge.classList.remove('hidden');
                    } else if (market.pre_bull_active) {
                        accelBadge.textContent = `⚡ E3b 季線總攻 (${market.target_today.toFixed(1)}x)`;
                        accelBadge.classList.remove('hidden');
                    }
                    const deepBadge = clone.querySelector('.deep-badge');
                    if (market.deep_bear_active) {
                        deepBadge.textContent = `🚨 C1 深熊減半 (${market.target_today.toFixed(1)}x)`;
                        deepBadge.classList.remove('hidden');
                    }
                    const staleBadge = clone.querySelector('.stale-badge');
                    if (staleBadge && (market.is_stale || market.fallback_used)) {
                        staleBadge.textContent = `⚠️ 資料延遲 (${market.date} vs 應有 ${market.expected_session || '--'})`;
                        staleBadge.classList.remove('hidden');
                    }

                    // 今日動作對比 (F11: 區分換檔、當日觸發模擬超標修剪、一般維持)
                    const deltaBadge = clone.querySelector('.delta-badge');
                    if (market.target_today !== market.target_yesterday) {
                        deltaBadge.textContent = `⚡ 調倉 ${market.target_yesterday.toFixed(2)}x ➔ ${market.target_today.toFixed(2)}x`;
                        deltaBadge.classList.add('change');
                    } else if (isTrimToday) {
                        deltaBadge.textContent = `✂️ 今日模擬修剪 (${market.last_trim.from_lev.toFixed(2)}x➔${market.target_today.toFixed(2)}x)`;
                        deltaBadge.classList.add('change');
                    } else {
                        deltaBadge.textContent = `維持 ${market.target_today.toFixed(2)}x`;
                        deltaBadge.classList.add('hold');
                    }

                    // 三大核心數據
                    clone.querySelector('.price').textContent = market.close.toFixed(2);
                    const biasPct = ((market.close / market.sma_slow_val - 1) * 100).toFixed(1);
                    clone.querySelector('.ma-dist-text').textContent = `年線乖離 ${biasPct >= 0 ? '+' : ''}${biasPct}%`;

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

                    // 2x2 技術防線矩陣
                    const highBwElem = clone.querySelector('.high-bw-val');
                    const isD1Frozen = (market.ticker === 'QQQ') && market.in_trend && market.sma_bear_val && (market.sma_fast_val < market.sma_bear_val);
                    if (!market.in_trend) {
                        if (market.pre_bull_active) {
                            const bearGuardVal = (market.sma_bear_val || market.sma_slow_val).toFixed(1);
                            highBwElem.textContent = `⚡ 季線總攻中 (守季線 ≥${bearGuardVal} / 攻年線 >${market.sma_slow_val.toFixed(1)})`;
                            highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                        } else if (market.sma_bear_val && market.close >= market.sma_bear_val && market.sma_fast_val >= market.sma_bear_val) {
                            highBwElem.textContent = `⚡ E3b 總攻 > ${market.high_bw.toFixed(2)}`;
                            highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                        } else {
                            highBwElem.textContent = `站上年線 > ${market.sma_slow_val.toFixed(2)}`;
                            highBwElem.className = 'tech-v font-mono warn-text high-bw-val';
                        }
                    } else if (market.ext_guard_active) {
                        highBwElem.textContent = `🌡️ 過熱封頂 ${extCapVal}x (>${market.high_bw.toFixed(1)})`;
                        highBwElem.className = 'tech-v font-mono warn-text high-bw-val';
                    } else if (market.step_idx >= market.max_steps) {
                        highBwElem.textContent = `🔒 已達 ${market.max_leverage.toFixed(1)}x 滿載上限`;
                        highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                    } else if (isD1Frozen) {
                        highBwElem.textContent = `🛡️ D1 凍結 (待 5MA ≥ ${market.sma_bear_val.toFixed(1)})`;
                        highBwElem.className = 'tech-v font-mono warn-text high-bw-val';
                    } else {
                        highBwElem.textContent = `> ${market.high_bw.toFixed(2)}`;
                        highBwElem.className = 'tech-v font-mono bull-text high-bw-val';
                    }

                    const lowBwElem = clone.querySelector('.low-bw-val');
                    if (!market.in_trend || market.step_idx === 0) {
                        lowBwElem.textContent = `🛡️ 已處底倉 (守年線 ${market.sma_slow_val.toFixed(1)})`;
                    } else {
                        lowBwElem.textContent = `< ${market.low_bw.toFixed(2)}`;
                    }

                    clone.querySelector('.fast-slow-ma').textContent = `${market.sma_fast_val.toFixed(1)} / ${market.sma_slow_val.toFixed(1)}`;
                    const c1Thresh = (market.sma_slow_val * (market.deep_bear_ratio || 0.97)).toFixed(1);
                    clone.querySelector('.bear-c1-ma').textContent = market.sma_bear_val
                        ? `${market.sma_bear_val.toFixed(1)} / ${c1Thresh}`
                        : '--';

                    const fmtEvent = (evt) => {
                        if (!evt) return '無紀錄';
                        const dTag = evt.days_ago === 0 ? '今日' : `${evt.days_ago}日前`;
                        return `${evt.date} (${dTag}) ${evt.from_lev.toFixed(2)}x➔${evt.to_lev.toFixed(2)}x [${evt.type}]`;
                    };
                    const lastActElem = clone.querySelector('.last-action-val');
                    if (lastActElem) {
                        lastActElem.textContent = fmtEvent(market.last_action);
                    }
                    const lastTrimElem = clone.querySelector('.last-trim-val');
                    if (lastTrimElem) {
                        lastTrimElem.textContent = fmtEvent(market.last_trim);
                    }

                    clone.querySelector('.strategy-explanation').textContent = market.explanation;

                    // 實戰部位與 DCA 試算器 (F04/F05/F11/F20: 100% 對齊 engine.run Step 4 單一淨指令；缺欄位時嚴禁反推實質槓桿或確定淨單)
                    const equityInput = clone.querySelector('.equity-input');
                    const positionInput = clone.querySelector('.position-input');
                    const dcaInput = clone.querySelector('.dca-input');
                    const resultBox = clone.querySelector('.action-result');
                    const summaryDiv = clone.querySelector('.calc-summary');
                    const instructionDiv = clone.querySelector('.action-instruction');
                    const savedPill = clone.querySelector('.saved-lev-pill');
                    const calcDrawer = clone.querySelector('.calc-drawer');
                    const savedMetaNote = clone.querySelector('.saved-meta-note');

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
                                savedMetaNote.textContent = `💾 實帳記憶時間: ${nowIso} (對應訊號日: ${market.date || '--'})`;
                            }
                        } catch (e) {}
                    }

                    function renderConditionalPreview() {
                        const targetToday = market.target_today;
                        const eqRaw = parseFloat(equityInput.value);
                        const posRaw = parseFloat(positionInput.value);
                        const dcaRaw = dcaInput ? parseFloat(dcaInput.value) : 0;
                        const hasEq = !isNaN(eqRaw) && eqRaw > 0;
                        const hasPos = !isNaN(posRaw) && posRaw > 0;
                        const dcaVal = (!isNaN(dcaRaw) && dcaRaw > 0) ? dcaRaw : 0;

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
                                `<div class="lev-check lev-ok">📐 <strong>【條件試算預覽（尚未輸入目前實帳總曝險）】</strong>若以淨值 <strong>$${fmt(eqAfter)}</strong> 按今日目標 <strong>${targetToday.toFixed(2)}x</strong> 試算，條件目標總曝險為 <strong>$${fmt(condTargetPos)}</strong>。因缺少目前實帳總曝險，系統不推斷實質槓桿或確定買賣金額；請補填「目前總曝險」以計算調倉差額。</div>`;
                        } else if (!hasEq && hasPos) {
                            const condReqEq = Math.round(posRaw / targetToday);
                            summaryDiv.innerHTML =
                                `已填目前總曝險: <strong>$${fmt(posRaw)}</strong>${dcaVal > 0 ? ` ｜ 擬投入新資金: <strong>$${fmt(dcaVal)}</strong>` : ''} ｜ ` +
                                `對應 ${targetToday.toFixed(2)}x 所需基準淨值: <strong>$${fmt(condReqEq)}</strong>`;
                            instructionDiv.innerHTML =
                                `<div class="lev-check lev-ok">📐 <strong>【條件試算預覽（尚未輸入本市場分配淨值 NAV）】</strong>目前已填總曝險 <strong>$${fmt(posRaw)}</strong>${dcaVal > 0 ? ` 與新資金 <strong>$${fmt(dcaVal)}</strong>` : ''}，惟缺少本市場真實淨值 (NAV)，系統嚴禁由目標反推實質槓桿或確定買賣金額（若欲維持 ${targetToday.toFixed(2)}x 需具備約 <strong>$${fmt(condReqEq)}</strong> 淨值）；請補填「分配淨值」以整合計算。</div>`;
                        } else {
                            const dcaExposure = Math.round(dcaVal * targetToday);
                            summaryDiv.innerHTML = `目標槓桿: <strong>${targetToday.toFixed(2)}x</strong> ｜ 本月分配新資金: <strong>$${fmt(dcaVal)}</strong>`;
                            instructionDiv.innerHTML = `<div class="lev-check lev-ok">💡【僅輸入新資金條件試算（請補填本市場淨值與目前曝險以整合修剪）】單獨新資金 <strong>$${fmt(dcaVal)}</strong> × <strong>${targetToday.toFixed(2)}x</strong> = 條件增量曝險 <strong>$${fmt(dcaExposure)}</strong>（未含舊倉修剪檢定）。</div>`;
                        }
                    }

                    function calculateAction(equity, position) {
                        const targetToday = market.target_today;
                        const prevTarget = market.target_yesterday !== undefined ? market.target_yesterday : targetToday;
                        const rawDca = dcaInput ? parseFloat(dcaInput.value) : 0;
                        const dcaVal = (!isNaN(rawDca) && rawDca > 0) ? rawDca : 0;

                        // Step 3 & Step 4 (engine.py 對齊)：先注入同幣別新資金更新分母 E' = E + C
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
                                levCheck = `<div class="lev-check lev-danger">📈 <strong>【系統升檔加碼 ${prevTarget.toFixed(2)}x ➔ ${targetToday.toFixed(2)}x】</strong>今日目標上調！入金後原始槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 低於新目標。<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險調至 <strong>$${fmt(finalTargetPos)}</strong>，調倉後槓桿 <strong>${plannedL.toFixed(2)}x</strong>，扣 1bp 摩擦後約 ${postFeeLev.toFixed(2)}x）。</div>`;
                            } else if (netOrder < 0) {
                                levCheck = `<div class="lev-check lev-danger">⚠️ <strong>【系統升檔 ${prevTarget.toFixed(2)}x ➔ ${targetToday.toFixed(2)}x · 實帳已超標須下修】</strong>今日目標雖升至 <strong>${targetToday.toFixed(2)}x</strong>，但你目前實際槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 高於新目標（修剪警戒 ${trimLine.toFixed(2)}x）！<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險由 $${fmt(position)} 壓回 <strong>$${fmt(finalTargetPos)}</strong>，執行後槓桿 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            } else {
                                levCheck = `<div class="lev-check lev-ok">✅ <strong>【系統升檔 ${prevTarget.toFixed(2)}x ➔ ${targetToday.toFixed(2)}x】</strong>目前實際槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 已精確對齊目標，${orderTag}。</div>`;
                            }
                        } else if (isStepDn) {
                            if (sigReb && netOrder < 0) {
                                levCheck = `<div class="lev-check lev-danger">📉 <strong>【系統降檔減碼 ${prevTarget.toFixed(2)}x ➔ ${targetToday.toFixed(2)}x】</strong>今日目標下調且入金後實際槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 高於新目標（換檔日不適用 +0.15x 緩衝）！<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險壓回 <strong>$${fmt(finalTargetPos)}</strong>，執行後槓桿 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            } else {
                                levCheck = `<div class="lev-check lev-ok">✅ <strong>【系統降檔 · 自然衰退免動作 (No-Buy-Down)】</strong>今日目標雖由 ${prevTarget.toFixed(2)}x 降至 <strong>${targetToday.toFixed(2)}x</strong>，但目前實際槓桿 <strong>${levAfterCash.toFixed(2)}x</strong> 已低於新目標。<br>🛡️ <strong>單一淨指令：${orderTag}</strong>（嚴禁逆勢加買補倉，維持總曝險 <strong>$${fmt(finalTargetPos)}</strong>，槓桿 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            }
                        } else {
                            // 今日系統目標槓桿未變 (維持原位階)
                            if (trimTriggered) {
                                levCheck = `<div class="lev-check lev-danger">🚨 <strong>【超標修剪與 DCA 整合淨指令】</strong>入金前槓桿 <strong>${actualLevBeforeDca.toFixed(2)}x</strong>${dcaVal > 0 ? `（注入新資金 $${fmt(dcaVal)} 並按目標加買後計畫槓桿將達 ${rawPlannedL.toFixed(2)}x）` : ''}，突破修剪線 <strong>${trimLine.toFixed(2)}x</strong>！<br>⚡ <strong>單一淨指令：${orderTag}</strong>（總曝險由 $${fmt(position)} 嚴格壓回 <strong>$${fmt(finalTargetPos)}</strong>，執行後槓桿 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            } else if (dcaVal > 0) {
                                levCheck = `<div class="lev-check lev-ok">💡 <strong>【DCA 新資金單向投入整合淨指令】</strong>入金前槓桿 <strong>${actualLevBeforeDca.toFixed(2)}x</strong>，注入新資金 <strong>$${fmt(dcaVal)}</strong> 後稀釋為 <strong>${levAfterCash.toFixed(2)}x</strong>；僅將新資金按 <strong>${targetToday.toFixed(2)}x</strong> 建倉後計畫槓桿為 <strong>${plannedL.toFixed(2)}x</strong>（≤ 修剪線 ${trimLine.toFixed(2)}x，免修剪舊倉）。<br>✅ <strong>單一淨指令：${orderTag}</strong>（目標總曝險 <strong>$${fmt(finalTargetPos)}</strong>，執行後槓桿 <strong>${plannedL.toFixed(2)}x</strong>）。</div>`;
                            } else if (actualLevBeforeDca < targetToday) {
                                levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLevBeforeDca.toFixed(2)}x</strong>（低於目標 ${targetToday.toFixed(2)}x）— 自然衰退中，<strong>單一淨指令：${orderTag}</strong>，讓利潤奔跑。</div>`;
                            } else {
                                levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLevBeforeDca.toFixed(2)}x</strong>（介於 ${targetToday.toFixed(2)}x ～ ${trimLine.toFixed(2)}x 緩衝帶內）— <strong>單一淨指令：${orderTag}</strong>。</div>`;
                            }
                        }

                        instructionDiv.innerHTML = levCheck;
                    }

                    function evaluateInputs(shouldPersistReal) {
                        syncSessionFormState();
                        const eqVal = equityInput.value.trim();
                        const posVal = positionInput.value.trim();
                        const equity = parseFloat(eqVal);
                        const position = parseFloat(posVal);
                        const hasBothReal = (eqVal !== '' && !isNaN(equity) && equity > 0 && posVal !== '' && !isNaN(position) && position > 0);
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
                    if (calcDrawer) {
                        calcDrawer.addEventListener('toggle', () => syncSessionFormState());
                    }

                    // F11/R3: 優先恢復本頁面工作階段 (Session) 中正在編輯的表單狀態（含 DCA 欄位），避免 5 分鐘自動重刷沖掉試算
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
                                savedMetaNote.textContent = `💾 實帳記憶時間: ${saved.savedAt}${staleSaved ? ' ⚠️ (跨日舊部位，請更新今日收盤淨值)' : ''}`;
                            }
                        } catch (e) {}
                        evaluateInputs(false);
                    } else {
                        // 載入 localStorage 記憶的淨值與部位 (F20: 顯示記憶時間與跨日提醒)
                        try {
                            const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
                            if (saved && saved.equity && saved.position) {
                                equityInput.value = saved.equity;
                                positionInput.value = saved.position;
                                if (savedMetaNote) {
                                    const staleSaved = saved.marketDate && market.date && saved.marketDate !== market.date;
                                    savedMetaNote.textContent = saved.savedAt
                                        ? `💾 實帳記憶時間: ${saved.savedAt}${staleSaved ? ' ⚠️ (跨日舊部位，請更新今日收盤淨值)' : ''}`
                                        : '💾 已載入上次記憶部位（建議確認是否為最新收盤實帳）';
                                }
                                evaluateInputs(false);
                            }
                        } catch (e) {}
                    }

                    dashboard.appendChild(clone);
                });

                // F12: 頂部同時顯示雙市場收盤日，若跨市場日期不一致則明確標示
                if (marketDates.length >= 2) {
                    const allSameDate = marketDates.every(m => m.date === marketDates[0].date);
                    const pairsStr = marketDates.map(m => `${m.label}: ${m.date}`).join(' ｜ ');
                    dataDateTag.textContent = allSameDate
                        ? `EOD: ${marketDates[0].date} (${pairsStr})`
                        : `EOD: ${pairsStr} ⚠️ 跨市場日期不同步`;
                } else if (marketDates.length === 1) {
                    dataDateTag.textContent = `EOD: ${marketDates[0].label} ${marketDates[0].date} ⚠️ 單一市場缺失`;
                }

                // F03/F11: 頂部全域動作膠囊優先順序
                if (anyStaleOrFallback) {
                    globalActionPill.textContent = '⚠️ 部分市場資料延遲或缺失 · 請核對卡片收盤日期';
                    globalActionPill.className = 'action-pill warn';
                } else if (anyLevChanged) {
                    globalActionPill.textContent = '⚡ 今日目標槓桿變動 · 請掛限價單調倉';
                    globalActionPill.className = 'action-pill warn';
                } else if (anyTrimToday) {
                    globalActionPill.textContent = '⚠️ 今日觸發模擬超標修剪 · 請核對實帳槓桿是否突破修剪線';
                    globalActionPill.className = 'action-pill warn';
                } else {
                    globalActionPill.textContent = '🟢 今日目標槓桿不變 · 讓利潤奔跑';
                    globalActionPill.className = 'action-pill bull';
                }

                // F11: 定期自動重抓最新 EOD 訊號與轉倉雷達（保留既有輸入狀態）
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
                ['tw', 'us'].forEach(key => {
                    const item = data[key];
                    if (!item) return;

                    const card = document.createElement('div');
                    card.className = `rollover-card ${item.status_class}`;
                    const inputPlaceholder = key === 'tw' ? '手動輸入跨月報價點差 (如 -20 或 10)' : '手動輸入跨季報價點差 (如 240)';

                    card.innerHTML = `
                        <div class="ro-header-compact">
                            <div>
                                <span class="ro-market-name">${item.market}</span>
                                <span class="ro-contract-tag font-mono">${item.contract_info}</span>
                            </div>
                            <span class="status-badge ${item.status_class === 'bull' ? 'bull' : 'bear'}">${item.badge}</span>
                        </div>
                        <div class="ro-summary-bar">
                            <span class="ro-countdown-chip font-mono ${item.days_left <= 3 ? 'urgent' : ''}">
                                結算倒數 ${item.days_left}d (${item.settlement_date})
                            </span>
                            <span>${item.action}</span>
                        </div>
                        <div class="ro-input-row">
                            <label class="ro-input-label">⚡ 手動價差試算 <span class="ro-nq-base font-mono"></span></label>
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
                                verdict.innerHTML = `🚨 <strong>【今日 13:30 結算硬期限 · 立即換約】</strong>目前點差 ${spread >= 0 ? '+' : ''}${spread} 點，但今日即為最後結算日，嚴禁等待或延後！請務必於 13:30 前以跨月價差單完成換約避免部位中斷！`;
                            } else if (spread <= 0) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🔥 <strong>逆價差送分題 (${spread} 點)</strong>：遠月折價，有利多頭掛跨月價差單換約！`;
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
                                verdict.innerHTML = `🚨 <strong>【結算硬期限 · 立即換約】</strong>目前估算年化隱含成本 ${annRate.toFixed(2)}%，但 NQ/MNQ 於結算日 09:30 ET 美股開盤即停止交易結算！嚴禁因點差偏高而延後，請立即以 Calendar Spread 完成換約！`;
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

    fetchData();
    fetchRolloverData();
});
