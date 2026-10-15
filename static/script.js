document.addEventListener('DOMContentLoaded', () => {
    const dashboard = document.getElementById('dashboard');
    const template = document.getElementById('market-card-template');

    // 自動判斷執行環境：在 GitHub Pages 上讀取靜態 JSON，在本地 Flask 上讀取 /api 端點
    const isStatic = window.location.hostname.includes('github.io') || window.location.protocol === 'file:';
    const DATA_API_URL = isStatic ? './api_data.json' : '/api/data';
    const ROLLOVER_API_URL = isStatic ? './api_rollover.json' : '/api/rollover';

    function fetchData(retryCount = 0) {
        fetch(DATA_API_URL + '?t=' + new Date().getTime())
            .then(response => {
                if (response.status === 202) {
                    // 伺服器正在預熱快取，30 秒後自動重試
                    return response.json().then(body => {
                        const msg = body.message || '資料載入中...';
                        const remaining = 30 - retryCount * 5;
                        dashboard.innerHTML = `
                            <div style="text-align:center; color:#94a3b8; padding:40px;">
                                <div style="font-size:2rem; margin-bottom:16px;">⏳</div>
                                <div style="font-size:1.1rem; margin-bottom:8px;">${msg}</div>
                                <div style="font-size:0.85rem; color:#64748b;">約 ${Math.max(remaining,5)} 秒後自動重試... (第 ${retryCount+1} 次)</div>
                            </div>`;
                        setTimeout(() => fetchData(retryCount + 1), 5000);
                        return null;
                    });
                }
                return response.json();
            })
            .then(data => {
                if (!data) return; // 202 狀態，等重試

                dashboard.innerHTML = '';
                dashboard.classList.remove('loading-state');

                data.forEach(market => {
                    if(!market) return;
                    if (market.ticker === 'QQQ' && market.close) {
                        window.latestQqqClose = market.close;
                        const nqBaseSpan = document.querySelector('.ro-nq-base');
                        if (nqBaseSpan) {
                            nqBaseSpan.textContent = `(基準點位: ~${Math.round(market.close * 41.15)} 點)`;
                        }
                    }

                    const clone = template.content.cloneNode(true);

                    clone.querySelector('.market-name').textContent = market.name;

                    const badge = clone.querySelector('.status-badge');
                    if (market.in_trend) {
                        badge.textContent = '🟢 牛市';
                        badge.classList.add('bull');
                    } else {
                        badge.textContent = '🔴 防禦中';
                        badge.classList.add('bear');
                    }

                    // 乖離過熱冷卻器 badge (美股專屬)
                    if (market.ext_guard_active) {
                        const extBadge = document.createElement('span');
                        extBadge.className = 'status-badge ext-guard';
                        extBadge.textContent = `🌡️ 過熱冷卻中 (乖離 ${((market.ext_ratio - 1) * 100).toFixed(1)}%)`;
                        extBadge.style.cssText = 'background:#ff6b35;color:#fff;font-size:0.75rem;padding:2px 8px;border-radius:4px;margin-left:6px;';
                        badge.parentNode.insertBefore(extBadge, badge.nextSibling);
                    } else if (market.ext_ratio && market.ext_ratio > 1.0 && market.ticker === 'QQQ') {
                        const extInfo = document.createElement('span');
                        extInfo.style.cssText = 'font-size:0.7rem;color:#888;margin-left:6px;';
                        extInfo.textContent = `乖離 ${((market.ext_ratio - 1) * 100).toFixed(1)}%`;
                        badge.parentNode.insertBefore(extInfo, badge.nextSibling);
                    }

                    // 牛初加速 badge (台股專屬)
                    if (market.bull_accel_active) {
                        const accelBadge = document.createElement('span');
                        accelBadge.className = 'status-badge bull-accel';
                        accelBadge.textContent = `🚀 牛初加速中 (${market.bull_accel_days_left}天)`;
                        accelBadge.style.cssText = 'background:#8b5cf6;color:#fff;font-size:0.75rem;padding:2px 8px;border-radius:4px;margin-left:6px;';
                        badge.parentNode.insertBefore(accelBadge, badge.nextSibling);
                    }

                    // 深熊二階段減半 badge (C1 防禦)
                    if (market.deep_bear_active) {
                        const deepBadge = document.createElement('span');
                        deepBadge.className = 'status-badge deep-bear';
                        deepBadge.textContent = `🚨 深熊減半 (${market.target_today.toFixed(1)}x)`;
                        deepBadge.style.cssText = 'background:#dc2626;color:#fff;font-size:0.75rem;padding:2px 8px;border-radius:4px;margin-left:6px;';
                        badge.parentNode.insertBefore(deepBadge, badge.nextSibling);
                    }

                    clone.querySelector('.price').textContent = market.close.toFixed(2);
                    clone.querySelector('.target-lev').textContent = market.target_today.toFixed(2) + 'x';
                    clone.querySelector('.steps').textContent = `${market.step_idx} / ${market.max_steps}`;

                    const trimTag = clone.querySelector('.trim-trigger-tag');
                    if (trimTag && market.trim_trigger_lev) {
                        trimTag.innerHTML = `修剪線: <strong style="color:#f59e0b;">${market.trim_trigger_lev.toFixed(2)}x</strong>`;
                    }

                    clone.querySelector('.fast-ma').innerHTML = `${market.fast_ma_len}MA: <span>${market.sma_fast_val.toFixed(2)}</span>`;
                    clone.querySelector('.slow-ma').innerHTML = `${market.slow_ma_len}MA: <span>${market.sma_slow_val.toFixed(2)}</span>`;

                    const bearMaElem = clone.querySelector('.bear-ma');
                    if (bearMaElem && market.sma_bear_val) {
                        const bearThresh = (market.sma_slow_val * (market.deep_bear_ratio || 0.97)).toFixed(2);
                        bearMaElem.innerHTML = `60MA: <span>${market.sma_bear_val.toFixed(2)}</span> <span style="font-size:11px; color:#ef4444; margin-left:3px;">(深熊門檻: ${bearThresh})</span>`;
                    } else if (bearMaElem) {
                        bearMaElem.style.display = 'none';
                    }

                    const trimBadge = clone.querySelector('.trim-badge');
                    if (trimBadge && market.trim_trigger_lev) {
                        trimBadge.innerHTML = `修剪警戒: <span style="color:#f59e0b;">>${market.trim_trigger_lev.toFixed(2)}x</span>`;
                    } else if (trimBadge) {
                        trimBadge.style.display = 'none';
                    }

                    clone.querySelector('.strategy-explanation').textContent = market.explanation;

                    const equityInput = clone.querySelector('.equity-input');
                    const positionInput = clone.querySelector('.position-input');
                    const dcaInput = clone.querySelector('.dca-input');
                    const resultBox = clone.querySelector('.action-result');
                    const summaryDiv = clone.querySelector('.calc-summary');
                    const instructionDiv = clone.querySelector('.action-instruction');

                    let lastEdited = null; // 'equity' or 'position'

                    const fmt = (n) => n.toLocaleString(undefined, {maximumFractionDigits:0});

                    // 輸入淨值 → 若無部位則自動反推，若已有部位則直接檢驗
                    equityInput.addEventListener('input', () => {
                        lastEdited = 'equity';
                        const equity = parseFloat(equityInput.value);
                        if (isNaN(equity) || equity <= 0) {
                            resultBox.classList.add('hidden');
                            return;
                        }
                        const posVal = positionInput.value.trim();
                        if (posVal === '') {
                            const targetPos = Math.round(equity * market.target_today);
                            positionInput.value = targetPos;
                            calculateAction(equity, targetPos);
                        } else {
                            const position = parseFloat(posVal);
                            if (isNaN(position) || position <= 0) {
                                const targetPos = Math.round(equity * market.target_today);
                                positionInput.value = targetPos;
                                calculateAction(equity, targetPos);
                            } else {
                                calculateAction(equity, position);
                            }
                        }
                    });

                    // 輸入部位 → 嚴禁覆寫 equityInput.value！
                    positionInput.addEventListener('input', () => {
                        lastEdited = 'position';
                        const position = parseFloat(positionInput.value);
                        if (isNaN(position) || position <= 0) {
                            resultBox.classList.add('hidden');
                            return;
                        }
                        const eqVal = equityInput.value.trim();
                        if (eqVal !== '') {
                            const equity = parseFloat(eqVal);
                            if (!isNaN(equity) && equity > 0) {
                                calculateAction(equity, position);
                                return;
                            }
                        }
                        // 若 equity 為空，才反推建議淨值
                        const neededEquity = Math.round(position / market.target_today);
                        equityInput.value = neededEquity;
                        calculateAction(neededEquity, position);
                    });

                    // 輸入本月新增資金 DCA
                    if (dcaInput) {
                        dcaInput.addEventListener('input', () => {
                            const equity = parseFloat(equityInput.value);
                            const position = parseFloat(positionInput.value);
                            if (!isNaN(equity) && equity > 0 && !isNaN(position) && position > 0) {
                                calculateAction(equity, position);
                            }
                        });
                    }

                    function calculateAction(equity, position) {
                        const targetToday = market.target_today;
                        const targetPos = equity * targetToday;
                        const actualLev = position / equity;
                        const trimLine = market.trim_trigger_lev || (targetToday + 0.15); // 固定 +0.15x 絕對容差帶

                        resultBox.classList.remove('hidden');
                        instructionDiv.className = 'action-instruction';

                        const marginAcct = Math.round(equity * 2/3);
                        const idleCash = Math.round(equity * 1/3);

                        // 判斷是否需要進行真實槓桿偏離檢驗 (部位與目標部位存在差異，或手動修改過部位)
                        const isDeviation = Math.abs(actualLev - targetToday) >= 0.01 || lastEdited === 'position';

                        let levCheck = '';
                        if (isDeviation) {
                            const diff = actualLev - targetToday;
                            if (actualLev < targetToday) {
                                levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong>（低於目標 ${targetToday.toFixed(2)}x）— 自然衰退中，<strong>完全不動作</strong>，讓利潤奔跑。</div>`;
                            } else if (actualLev <= trimLine) {
                                levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong>（目標 ${targetToday.toFixed(2)}x ～ 修剪線 ${trimLine.toFixed(2)}x 之間）— 落在 <strong>+0.15x 修剪緩衝帶內</strong>，日常雜訊不需碰它。</div>`;
                            } else {
                                const excessPos = Math.round(position - targetPos);
                                levCheck = `<div class="lev-check lev-danger">🚨 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong> 已突破修剪線 <strong>${trimLine.toFixed(2)}x</strong>！超標 +${diff.toFixed(2)}x<br>⚠️ <strong>需修剪回目標 ${targetToday.toFixed(2)}x</strong>（非修回 ${trimLine.toFixed(2)}x），應減持約 <strong>$${fmt(excessPos)}</strong> 部位，或用本月 DCA 資金自然稀釋。</div>`;
                            }
                        }

                        if (!isDeviation) {
                            summaryDiv.innerHTML = `
                                <div style="display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px;">
                                    <span>目標槓桿: <strong>${targetToday.toFixed(2)}x</strong></span>
                                    <span>目標部位: <strong>$${fmt(targetPos)}</strong></span>
                                    <span>修剪線: <strong>${trimLine.toFixed(2)}x</strong></span>
                                </div>`;
                            instructionDiv.innerHTML = `
                                💡 淨值 <strong>$${fmt(equity)}</strong> 在 ${targetToday.toFixed(2)}x 槓桿下，應持有 <strong>$${fmt(targetPos)}</strong> 的部位。<br>
                                🛡️ 保證金帳戶放 <strong>$${fmt(marginAcct)}</strong>，其餘 <strong>$${fmt(idleCash)}</strong> 放生息帳戶。`;
                            instructionDiv.classList.add('hold');
                        } else {
                            summaryDiv.innerHTML = `
                                <div style="display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px;">
                                    <span>目標槓桿: <strong>${targetToday.toFixed(2)}x</strong></span>
                                    <span>實際槓桿: <strong>${actualLev.toFixed(2)}x</strong></span>
                                    <span>修剪線: <strong>${trimLine.toFixed(2)}x</strong></span>
                                </div>`;
                            instructionDiv.innerHTML = `
                                💡 持有 <strong>$${fmt(position)}</strong> 部位 ÷ 淨值 <strong>$${fmt(equity)}</strong> = 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong><br>
                                🛡️ 保證金帳戶放 <strong>$${fmt(marginAcct)}</strong>，其餘 <strong>$${fmt(idleCash)}</strong> 放生息帳戶。`;
                            instructionDiv.classList.add(actualLev > trimLine ? 'sell' : 'hold');
                        }

                        if (levCheck) {
                            instructionDiv.innerHTML += levCheck;
                        }

                        // DCA 新增資金試算
                        const dcaVal = dcaInput ? parseFloat(dcaInput.value) : 0;
                        if (!isNaN(dcaVal) && dcaVal > 0) {
                            const dcaExposure = Math.round(dcaVal * targetToday);
                            instructionDiv.innerHTML += `<div class="dca-hint" style="margin-top:10px; padding:8px 12px; background:rgba(59,130,246,0.1); border-left:3px solid #3b82f6; border-radius:4px; font-size:13px; color:#93c5fd;">💡【新資金單向投入法】新增資金 <strong>$${fmt(dcaVal)}</strong> 按當日目標槓桿 <strong>${targetToday.toFixed(2)}x</strong>，應加買 <strong>$${fmt(dcaExposure)}</strong> 曝險（絕對不補舊部位的自然衰減）。</div>`;
                        }
                    }

                    dashboard.appendChild(clone);
                });
            })
            .catch(err => {
                dashboard.innerHTML = `<div style="text-align:center; color:#ef4444; padding:20px;">連線失敗，請確認伺服器與網路狀態。<br><br>${err.message}</div>`;
                console.error(err);
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

                    const inputPlaceholder = key === 'tw' 
                        ? '輸入券商價差 (例如: +10 或 -30)' 
                        : '輸入跨季價差點數 (例如: 180)';

                    card.innerHTML = `
                        <div class="ro-header-compact">
                            <div class="ro-title-left">
                                <span class="ro-market-name">${item.market}</span>
                                <span class="ro-contract-tag font-mono">${item.contract_info}</span>
                            </div>
                            <span class="status-badge ${item.status_class}">${item.badge}</span>
                        </div>

                        <div class="ro-summary-bar">
                            <span class="ro-countdown-chip ${item.days_left <= 3 ? 'urgent' : ''}">
                                倒數 <strong>${item.days_left}</strong> 天（${item.settlement_date} 結算）
                            </span>
                            <span class="ro-action-hint">${item.action}</span>
                        </div>

                        <div class="ro-quick-checker">
                            <div class="ro-input-row">
                                <label class="ro-input-label">⚡ 價差健檢 <span class="ro-nq-base" style="font-size:11px; color:#94a3b8; font-weight:normal;"></span></label>
                                <div class="ro-input-wrapper">
                                    <input type="number" class="ro-spread-input" placeholder="${inputPlaceholder}" step="any">
                                    <span class="ro-input-unit">點</span>
                                </div>
                            </div>
                            <div class="ro-verdict-compact hidden"></div>
                        </div>
                    `;

                    // 綁定極簡價差健檢
                    const input = card.querySelector('.ro-spread-input');
                    const verdict = card.querySelector('.ro-verdict-compact');

                    if (key === 'tw') {
                        input.addEventListener('input', () => {
                            const val = input.value.trim();
                            if (val === '') {
                                verdict.classList.add('hidden');
                                return;
                            }
                            const spread = parseFloat(val);
                            verdict.classList.remove('hidden');
                            if (spread <= 0) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🔥 <strong>逆價差送分題 (${spread} 點)</strong>：遠月比近月便宜，無腦掛【跨月價差單】直接換約，倒賺基差！`;
                            } else if (spread <= 15) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>價差極窄 (+${spread} 點)</strong>：摩擦成本極低，建議立即以【跨月價差單】掛買一換約。`;
                            } else if (spread <= 25) {
                                verdict.className = 'ro-verdict-compact neutral';
                                verdict.innerHTML = `🟡 <strong>常態價差 (+${spread} 點)</strong>：可觀察盤中急殺是否有更甜點位，結算前 1~2 天再換。`;
                            } else {
                                verdict.className = 'ro-verdict-compact bear';
                                verdict.innerHTML = `🔴 <strong>價差偏大 (+${spread} 點)</strong>：遠月溢價偏高，建議暫緩，等盤中急殺或結算前價差收斂。`;
                            }
                        });
                    } else {
                        const nqBaseSpan = card.querySelector('.ro-nq-base');
                        const getNqImplied = () => (window.latestQqqClose || 500) * 41.15;
                        if (nqBaseSpan) {
                            nqBaseSpan.textContent = `(基準點位: ~${Math.round(getNqImplied())} 點)`;
                        }

                        input.addEventListener('input', () => {
                            const val = input.value.trim();
                            if (val === '') {
                                verdict.classList.add('hidden');
                                return;
                            }
                            const spread = parseFloat(val);
                            const nqImplied = getNqImplied();
                            const annRate = (spread / nqImplied) * (365 / 91) * 100;
                            verdict.classList.remove('hidden');

                            if (annRate <= 0) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🔥 <strong>罕見逆價差 (年化 ${annRate.toFixed(1)}%)</strong>：次季比當季便宜，無腦掛單換約！`;
                            } else if (annRate <= 4.2) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>超值低價 (年化 ${annRate.toFixed(1)}%)</strong>：低於美債利率，建議以【Calendar Spread】掛 Mid-Price 換約！`;
                            } else if (annRate <= 4.8) {
                                verdict.className = 'ro-verdict-compact bull';
                                verdict.innerHTML = `🟢 <strong>合理定價 (年化 ${annRate.toFixed(1)}%)</strong>：符合市場常態，在 Roll Week 期間以價差單從容成交。`;
                            } else {
                                verdict.className = 'ro-verdict-compact bear';
                                verdict.innerHTML = `🔴 <strong>溢價偏貴 (年化 ${annRate.toFixed(1)}%)</strong>：遠月追高，建議等美股盤中拉回或價差收縮再換。`;
                            }
                        });
                    }

                    container.appendChild(card);
                });
            })
            .catch(err => {
                console.error('Rollover fetch error:', err);
                container.innerHTML = `<div style="color:#ef4444; padding:15px; text-align:center;">轉倉雷達連線失敗，請稍後重試</div>`;
            });
    }

    fetchData();
    fetchRolloverData();
});


