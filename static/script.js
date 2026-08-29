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

                        resultBox.classList.remove('hidden');
                        instructionDiv.className = 'action-instruction';

                        const marginAcct = Math.round(equity * 2/3);
                        const idleCash = Math.round(equity * 1/3);

                        if (lastEdited === 'equity') {
                            summaryDiv.innerHTML = `
                                <div style="display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px;">
                                    <span>目標槓桿: <strong>${targetToday.toFixed(2)}x</strong></span>
                                    <span>目標部位: <strong>$${fmt(targetPos)}</strong></span>
                                </div>`;
                            instructionDiv.innerHTML = `
                                💡 淨值 <strong>$${fmt(equity)}</strong> 在 ${targetToday}x 槓桿下，應持有 <strong>$${fmt(targetPos)}</strong> 的部位。<br>
                                🛡️ 保證金帳戶放 <strong>$${fmt(marginAcct)}</strong>，其餘 <strong>$${fmt(idleCash)}</strong> 放生息帳戶。`;
                            instructionDiv.classList.add('hold');
                        } else {
                            summaryDiv.innerHTML = `
                                <div style="display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px;">
                                    <span>目標槓桿: <strong>${targetToday.toFixed(2)}x</strong></span>
                                    <span>所需淨值: <strong>$${fmt(equity)}</strong></span>
                                </div>`;
                            instructionDiv.innerHTML = `
                                💡 持有 <strong>$${fmt(position)}</strong> 部位在 ${targetToday}x 槓桿下，需要淨值 <strong>$${fmt(equity)}</strong>。<br>
                                🛡️ 保證金帳戶放 <strong>$${fmt(marginAcct)}</strong>，其餘 <strong>$${fmt(idleCash)}</strong> 放生息帳戶。`;
                            instructionDiv.classList.add('hold');
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

    fetchData();
});


