document.addEventListener('DOMContentLoaded', () => {
    const dashboard = document.getElementById('dashboard');
    const template = document.getElementById('market-card-template');

    fetch('/api/data?t=' + new Date().getTime())
        .then(response => response.json())
        .then(data => {
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
                
                const equityInput = clone.querySelector('.equity-input');
                const positionInput = clone.querySelector('.position-input');
                const resultBox = clone.querySelector('.action-result');
                const currentLevSpan = clone.querySelector('.current-lev span');
                const instructionDiv = clone.querySelector('.action-instruction');
                
                const calculateAction = () => {
                    const equity = parseFloat(equityInput.value);
                    const position = parseFloat(positionInput.value);
                    
                    if (isNaN(equity) || isNaN(position) || equity <= 0) {
                        resultBox.classList.add('hidden');
                        return;
                    }
                    
                    const currentLev = position / equity;
                    currentLevSpan.textContent = currentLev.toFixed(2) + 'x';
                    resultBox.classList.remove('hidden');
                    
                    const targetToday = market.target_today;
                    const targetYtd = market.target_yesterday;
                    
                    instructionDiv.className = 'action-instruction'; 
                    
                    if (targetToday > targetYtd) {
                        const targetPos = equity * targetToday;
                        const buyAmt = targetPos - position;
                        instructionDiv.innerHTML = `📈 <strong>系統升級</strong><br>買進價值 $${buyAmt.toLocaleString(undefined, {maximumFractionDigits:0})} 的部位，將槓桿提升至 ${targetToday} 倍。`;
                        instructionDiv.classList.add('buy');
                    } else if (targetToday < targetYtd) {
                        if (currentLev > targetToday) {
                            const targetPos = equity * targetToday;
                            const sellAmt = position - targetPos;
                            instructionDiv.innerHTML = `📉 <strong>系統降級</strong><br>槓桿高於目標，請賣出 $${sellAmt.toLocaleString(undefined, {maximumFractionDigits:0})} 的部位。`;
                            instructionDiv.classList.add('sell');
                        } else {
                            instructionDiv.innerHTML = `🛡️ <strong>系統降級</strong><br>您的槓桿已在安全帶 (${targetToday} 倍以下)，無需動作！`;
                            instructionDiv.classList.add('hold');
                        }
                    } else {
                        if (currentLev > targetToday) {
                            const targetPos = equity * targetToday;
                            const sellAmt = position - targetPos;
                            instructionDiv.innerHTML = `⚠️ <strong>風險過高</strong><br>超過目標上限，請賣出 $${sellAmt.toLocaleString(undefined, {maximumFractionDigits:0})} 的部位。`;
                            instructionDiv.classList.add('sell');
                        } else {
                            if (market.step_idx === 0) {
                                instructionDiv.innerHTML = `🛡️ <strong>全面防禦狀態</strong><br>目前處於低動能環境，請維持 ${targetToday} 倍防禦底倉，耐心等待！`;
                            } else {
                                instructionDiv.innerHTML = `🧘 <strong>自然降槓桿紅利期</strong><br>狀態非常安全，完全無需動作，讓利潤奔跑！`;
                            }
                            instructionDiv.classList.add('hold');
                        }
                    }
                };
                
                equityInput.addEventListener('input', calculateAction);
                positionInput.addEventListener('input', calculateAction);
                
                dashboard.appendChild(clone);
            });
        })
        .catch(err => {
            dashboard.innerHTML = `<div style="text-align:center; color:#ef4444; padding:20px;">連線失敗，請確認伺服器與網路狀態。<br><br>${err.message}</div>`;
            console.error(err);
        });
});
