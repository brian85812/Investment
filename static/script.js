document.addEventListener('DOMContentLoaded', () => {
    const dashboard = document.getElementById('dashboard');
    const template = document.getElementById('market-card-template');

    function fetchData(retryCount = 0) {
        fetch('/api/data?t=' + new Date().getTime())
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

                    clone.querySelector('.price').textContent = market.close.toFixed(2);
                    clone.querySelector('.target-lev').textContent = market.target_today.toFixed(2) + 'x';
                    clone.querySelector('.steps').textContent = `${market.step_idx} / ${market.max_steps}`;

                    clone.querySelector('.fast-ma').innerHTML = `${market.fast_ma_len}MA: <span>${market.sma_fast_val.toFixed(2)}</span>`;
                    clone.querySelector('.slow-ma').innerHTML = `${market.slow_ma_len}MA: <span>${market.sma_slow_val.toFixed(2)}</span>`;
                    clone.querySelector('.strategy-explanation').textContent = market.explanation;

                    const equityInput = clone.querySelector('.equity-input');
                    const positionInput = clone.querySelector('.position-input');
                    const resultBox = clone.querySelector('.action-result');
                    const summaryDiv = clone.querySelector('.calc-summary');
                    const instructionDiv = clone.querySelector('.action-instruction');

                    let lastEdited = null; // 'equity' or 'position'

                    const fmt = (n) => n.toLocaleString(undefined, {maximumFractionDigits:0});

                    // 輸入淨值 → 自動反推「目標部位應為多少」
                    equityInput.addEventListener('input', () => {
                        lastEdited = 'equity';
                        const equity = parseFloat(equityInput.value);
                        if (isNaN(equity) || equity <= 0) {
                            resultBox.classList.add('hidden');
                            return;
                        }
                        const targetPos = equity * market.target_today;
                        positionInput.value = Math.round(targetPos);
                        calculateAction(equity, targetPos);
                    });

                    // 輸入部位 → 自動反推「需要多少淨值」
                    positionInput.addEventListener('input', () => {
                        lastEdited = 'position';
                        const position = parseFloat(positionInput.value);
                        if (isNaN(position) || position <= 0) {
                            resultBox.classList.add('hidden');
                            return;
                        }
                        const neededEquity = position / market.target_today;
                        equityInput.value = Math.round(neededEquity);
                        calculateAction(neededEquity, position);
                    });

                    function calculateAction(equity, position) {
                        const targetToday = market.target_today;
                        const targetPos = equity * targetToday;
                        const actualLev = position / equity;
                        const trimLine = targetToday * 1.05; // 5% 相對容差帶

                        resultBox.classList.remove('hidden');
                        instructionDiv.className = 'action-instruction';

                        const marginAcct = Math.round(equity * 2/3);
                        const idleCash = Math.round(equity * 1/3);

                        // 槓桿健檢區塊
                        let levCheck = '';
                        if (lastEdited === 'position') {
                            const diff = actualLev - targetToday;
                            if (actualLev < targetToday) {
                                levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong>（低於目標 ${targetToday.toFixed(2)}x）— 自然衰退中，<strong>完全不動作</strong>，讓利潤奔跑。</div>`;
                            } else if (actualLev <= trimLine) {
                                levCheck = `<div class="lev-check lev-ok">✅ 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong>（目標 ${targetToday.toFixed(2)}x ～ 修剪線 ${trimLine.toFixed(2)}x 之間）— 落在 <strong>5% 容差帶內</strong>，日常雜訊不需碰它。</div>`;
                            } else {
                                const excessPos = Math.round(position - targetPos);
                                levCheck = `<div class="lev-check lev-danger">🚨 實際槓桿 <strong>${actualLev.toFixed(2)}x</strong> 已突破修剪線 <strong>${trimLine.toFixed(2)}x</strong>！超標 +${diff.toFixed(2)}x<br>⚠️ <strong>需修剪回目標 ${targetToday.toFixed(2)}x</strong>（非修回 ${trimLine.toFixed(2)}x），應減持約 <strong>$${fmt(excessPos)}</strong> 部位，或用本月 DCA 資金自然稀釋。</div>`;
                            }
                        }

                        if (lastEdited === 'equity') {
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

        fetch('/api/rollover?t=' + new Date().getTime())
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
                                <label class="ro-input-label">⚡ 價差健檢</label>
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
                        input.addEventListener('input', () => {
                            const val = input.value.trim();
                            if (val === '') {
                                verdict.classList.add('hidden');
                                return;
                            }
                            const spread = parseFloat(val);
                            // 預設以 20000 點基準換算 91 天年化
                            const annRate = (spread / 20000) * (365 / 91) * 100;
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


