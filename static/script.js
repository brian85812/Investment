document.addEventListener('DOMContentLoaded', () => {
    const dashboard = document.getElementById('dashboard');
    const template = document.getElementById('market-card-template');
    const globalActionPill = document.getElementById('global-action-pill');
    const dataDateTag = document.getElementById('data-date-tag');

    const isStatic = window.location.hostname.includes('github.io') || window.location.protocol === 'file:';
    const DATA_API_URL = isStatic ? './api_data.json' : '/api/data';
    const ROLLOVER_API_URL = isStatic ? './api_rollover.json' : '/api/rollover';

    const fmt = (n) => Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });

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

                let anyLevChanged = false;
                let latestDate = '';

                data.forEach(market => {
                    if (!market) return;
                    if (market.date) latestDate = market.date;
                    if (market.target_today !== market.target_yesterday) anyLevChanged = true;

                    if (market.ticker === 'QQQ' && market.close) {
                        window.latestQqqClose = market.close;
                        const nqBaseSpan = document.querySelector('.ro-nq-base');
                        if (nqBaseSpan) {
                            nqBaseSpan.textContent = `(~${Math.round(market.close * 41.15)}點)`;
                        }
                    }

                    const clone = template.content.cloneNode(true);
                    const storageKey = `moat_calc_${market.id}`;

                    // 標題與狀態標籤
                    clone.querySelector('.market-name').textContent = market.name;
                    clone.querySelector('.ticker-code').textContent = market.ticker;

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

                    // 今日動作對比
                    const deltaBadge = clone.querySelector('.delta-badge');
                    if (market.target_today !== market.target_yesterday) {
                        deltaBadge.textContent = `⚡ 調倉 ${market.target_yesterday.toFixed(2)}x ➔ ${market.target_today.toFixed(2)}x`;
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
                            highBwElem.textContent = `⚡ 季線總攻中 (守年線 >${market.sma_slow_val.toFixed(1)})`;
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

                    clone.querySelector('.strategy-explanation').textContent = market.explanation;

                    // 實戰部位與 DCA 試算器 (含 localStorage 自動記憶)
                    const equityInput = clone.querySelector('.equity-input');
                    const positionInput = clone.querySelector('.position-input');
                    const dcaInput = clone.querySelector('.dca-input');
                    const resultBox = clone.querySelector('.action-result');
                    const summaryDiv = clone.querySelector('.calc-summary');
                    const instructionDiv = clone.querySelector('.action-instruction');
                    const savedPill = clone.querySelector('.saved-lev-pill');
                    const calcDrawer = clone.querySelector('.calc-drawer');

                    let lastEdited = null;

                    function saveState() {
                        try {
                            localStorage.setItem(storageKey, JSON.stringify({
                                equity: equityInput.value,
                                position: positionInput.value
                            }));
                        } catch (e) {}
                    }

                    function renderDcaOnly(dcaVal) {
                        const targetToday = market.target_today;
                        const dcaExposure = Math.round(dcaVal * targetToday);
                        resultBox.classList.remove('hidden');
                        summaryDiv.innerHTML = `目標槓桿: <strong>${targetToday.toFixed(2)}x</strong> ｜ 本月新資金: <strong>$${fmt(dcaVal)}</strong>`;
                        instructionDiv.innerHTML = `<div class="lev-check lev-ok">💡【新資金單向投入法】新增資金 <strong>$${fmt(dcaVal)}</strong> 按當日目標槓桿 <strong>${targetToday.toFixed(2)}x</strong>，應加買 <strong>$${fmt(dcaExposure)}</strong> 曝險（絕對不補舊部位的自然衰減）。</div>`;
                    }

                    function calculateAction(equity, position) {
                        const targetToday = market.target_today;
                        const targetPos = equity * targetToday;
                        const actualLev = position / equity;
                        const marginAcct = Math.round(equity * 2 / 3);
                        const idleCash = Math.round(equity * 1 / 3);

                        resultBox.classList.remove('hidden');
                        savedPill.classList.remove('hidden', 'danger', 'ok');
                        savedPill.textContent = `實質 ${actualLev.toFixed(2)}x`;
                        savedPill.classList.add(actualLev > trimLine ? 'danger' : 'ok');

                        if (actualLev > trimLine) {
                            calcDrawer.open = true; // 超標時自動展開警示
                        }

                        summaryDiv.innerHTML = `目標部位: <strong>$${fmt(targetPos)}</strong> ｜ 2/3 保證金: <strong>$${fmt(marginAcct)}</strong> ｜ 1/3 防禦金: <strong>$${fmt(idleCash)}</strong>`;

                        let levCheck = '';
                        const diff = actualLev - targetToday;
                        if (Math.abs(diff) < 0.01 && lastEdited === 'equity') {
                            levCheck = `<div class="lev-check lev-ok">✅ 標準建倉：淨值 <strong>$${fmt(equity)}</strong> 對應 <strong>${targetToday.toFixed(2)}x</strong> 目標曝險為 <strong>$${fmt(targetPos)}</strong>。</div>`;
                        } else if (actualLev < targetToday) {
                            levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong>（低於目標 ${targetToday.toFixed(2)}x）— 自然衰退中，<strong>完全不動作</strong>，讓利潤奔跑。</div>`;
                        } else if (actualLev <= trimLine) {
                            levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong>（介於 ${targetToday.toFixed(2)}x ～ ${trimLine.toFixed(2)}x）— 落在 <strong>+0.15x 緩衝帶內</strong>，無須動作。</div>`;
                        } else {
                            const excessPos = Math.round(position - targetPos);
                            levCheck = `<div class="lev-check lev-danger">🚨 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong> 突破修剪線 <strong>${trimLine.toFixed(2)}x</strong>（超標 +${diff.toFixed(2)}x）！<br>⚠️ <strong>須直接砍回目標 ${targetToday.toFixed(2)}x</strong>：請減持約 <strong>$${fmt(excessPos)}</strong> 曝險，或以本月 DCA 新資金自然稀釋。</div>`;
                        }

                        instructionDiv.innerHTML = levCheck;

                        const dcaVal = dcaInput ? parseFloat(dcaInput.value) : 0;
                        if (!isNaN(dcaVal) && dcaVal > 0) {
                            const dcaExposure = Math.round(dcaVal * targetToday);
                            instructionDiv.innerHTML += `<div class="lev-check lev-ok" style="margin-top:8px;">💡【新資金單向投入】新增 <strong>$${fmt(dcaVal)}</strong> × <strong>${targetToday.toFixed(2)}x</strong> = 應加買 <strong>$${fmt(dcaExposure)}</strong> 曝險（不補舊倉衰減）。</div>`;
                        }
                    }

                    equityInput.addEventListener('input', () => {
                        lastEdited = 'equity';
                        saveState();
                        const equity = parseFloat(equityInput.value);
                        if (isNaN(equity) || equity <= 0) {
                            const dcaVal = parseFloat(dcaInput.value);
                            if (!isNaN(dcaVal) && dcaVal > 0) renderDcaOnly(dcaVal);
                            else resultBox.classList.add('hidden');
                            return;
                        }
                        const posVal = positionInput.value.trim();
                        if (posVal === '' || isNaN(parseFloat(posVal))) {
                            const targetPos = Math.round(equity * market.target_today);
                            positionInput.value = targetPos;
                            saveState();
                            calculateAction(equity, targetPos);
                        } else {
                            calculateAction(equity, parseFloat(posVal));
                        }
                    });

                    positionInput.addEventListener('input', () => {
                        lastEdited = 'position';
                        saveState();
                        const position = parseFloat(positionInput.value);
                        if (isNaN(position) || position <= 0) {
                            const dcaVal = parseFloat(dcaInput.value);
                            if (!isNaN(dcaVal) && dcaVal > 0) renderDcaOnly(dcaVal);
                            else resultBox.classList.add('hidden');
                            return;
                        }
                        const eqVal = equityInput.value.trim();
                        if (eqVal !== '' && !isNaN(parseFloat(eqVal)) && parseFloat(eqVal) > 0) {
                            calculateAction(parseFloat(eqVal), position);
                            return;
                        }
                        const neededEquity = Math.round(position / market.target_today);
                        equityInput.value = neededEquity;
                        saveState();
                        calculateAction(neededEquity, position);
                    });

                    dcaInput.addEventListener('input', () => {
                        const dcaVal = parseFloat(dcaInput.value);
                        const equity = parseFloat(equityInput.value);
                        const position = parseFloat(positionInput.value);
                        if (!isNaN(equity) && equity > 0 && !isNaN(position) && position > 0) {
                            calculateAction(equity, position);
                        } else if (!isNaN(dcaVal) && dcaVal > 0) {
                            renderDcaOnly(dcaVal);
                        } else {
                            resultBox.classList.add('hidden');
                        }
                    });

                    // 載入 localStorage 記憶的淨值與部位
                    try {
                        const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
                        if (saved && saved.equity && saved.position) {
                            equityInput.value = saved.equity;
                            positionInput.value = saved.position;
                            lastEdited = 'position';
                            calculateAction(parseFloat(saved.equity), parseFloat(saved.position));
                        }
                    } catch (e) {}

                    dashboard.appendChild(clone);
                });

                if (latestDate) dataDateTag.textContent = `EOD: ${latestDate}`;
                if (anyLevChanged) {
                    globalActionPill.textContent = '⚡ 今日目標槓桿變動 · 請掛限價單調倉';
                    globalActionPill.className = 'action-pill warn';
                } else {
                    globalActionPill.textContent = '🟢 今日目標槓桿不變 · 讓利潤奔跑';
                    globalActionPill.className = 'action-pill bull';
                }
            })
            .catch(err => {
                dashboard.innerHTML = `<div class="loading-box" style="color:#ef4444;">連線失敗：${err.message}</div>`;
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
                    const inputPlaceholder = key === 'tw' ? '輸入跨月點差 (如 -20 或 10)' : '輸入跨季點差 (如 240)';

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
                            <label class="ro-input-label">⚡ 價差試算 <span class="ro-nq-base font-mono"></span></label>
                            <div class="ro-input-wrapper">
                                <input type="number" class="ro-spread-input font-mono" placeholder="${inputPlaceholder}" step="any">
                                <span class="ro-input-unit">點</span>
                            </div>
                        </div>
                        <div class="ro-verdict-compact hidden"></div>
                    `;

                    const input = card.querySelector('.ro-spread-input');
                    const verdict = card.querySelector('.ro-verdict-compact');

                    if (key === 'tw') {
                        input.addEventListener('input', () => {
                            const val = input.value.trim();
                            if (val === '') { verdict.classList.add('hidden'); return; }
                            const spread = parseFloat(val);
                            verdict.classList.remove('hidden');
                            if (spread <= 0) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🔥 <strong>逆價差送分題 (${spread} 點)</strong>：遠月折價，無腦掛跨月價差單換約！`;
                            } else if (spread <= 15) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>價差極窄 (+${spread} 點)</strong>：摩擦極低，建議直接掛價差單換約。`;
                            } else if (spread <= 25) {
                                verdict.className = 'ro-verdict-compact neutral';
                                verdict.innerHTML = `🟡 <strong>常態價差 (+${spread} 點)</strong>：可等盤中急殺或結算前 1~2 天再換。`;
                            } else {
                                verdict.className = 'ro-verdict-compact bear';
                                verdict.innerHTML = `🔴 <strong>價差偏大 (+${spread} 點)</strong>：遠月溢價偏高，建議暫緩。`;
                            }
                        });
                    } else {
                        const nqBaseSpan = card.querySelector('.ro-nq-base');
                        const getNqImplied = () => (window.latestQqqClose || 700) * 41.15;
                        if (nqBaseSpan) nqBaseSpan.textContent = `(~${Math.round(getNqImplied())}點)`;

                        input.addEventListener('input', () => {
                            const val = input.value.trim();
                            if (val === '') { verdict.classList.add('hidden'); return; }
                            const spread = parseFloat(val);
                            const annRate = (spread / getNqImplied()) * (365 / 91) * 100;
                            verdict.classList.remove('hidden');
                            if (annRate <= 0) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🔥 <strong>罕見逆價差 (年化 ${annRate.toFixed(2)}%)</strong>：立即掛單換約！`;
                            } else if (annRate <= 4.2) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>超值低價 (年化 ${annRate.toFixed(2)}%)</strong>：低於短債利率，掛 Mid-Price 換約！`;
                            } else if (annRate <= 4.8) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>合理定價 (年化 ${annRate.toFixed(2)}%)</strong>：符合常態利率，於 Roll Week 從容換約。`;
                            } else {
                                verdict.className = 'ro-verdict-compact bear';
                                verdict.innerHTML = `🔴 <strong>溢價偏貴 (年化 ${annRate.toFixed(2)}%)</strong>：建議等拉回價差收斂再換。`;
                            }
                        });
                    }
                    container.appendChild(card);
                });
            });
    }

    fetchData();
    fetchRolloverData();
});
