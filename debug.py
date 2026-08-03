import yfinance as yf
from datetime import datetime, timedelta
import pandas as pd
from app import get_signal_data

qqq_data = get_signal_data(
    name="美股 QQQ", ticker="QQQ", 
    base_leverage=0.6, max_leverage=3.0,
    fast_ma=20, slow_ma=200, breakout_window=40, cooldown=5,
    allocs=[0.0, 0.5, 0.8, 1.0]
)

print(qqq_data)

# Let's manually print the last 20 days of calculations
ticker = 'QQQ'
end_date = datetime.now()
start_date = end_date - timedelta(days=5*365)
df = yf.Ticker(ticker).history(start=start_date)

fast_ma=20
slow_ma=200
breakout_window=40
cooldown=5
base_leverage=0.6
max_leverage=3.0
allocs=[0.0, 0.5, 0.8, 1.0]

df['SMA_20'] = df['Close'].rolling(window=fast_ma).mean()
df['SMA_200'] = df['Close'].rolling(window=slow_ma).mean()
df['High_40'] = df['High'].shift(1).rolling(window=breakout_window).max()
df['Low_40'] = df['Low'].shift(1).rolling(window=breakout_window).min()
df = df.dropna()

in_trend = False
step_idx = 0
last_action_idx = -999
target_history = []
max_idx = len(allocs) - 1

print(f"{'Date':<12} | {'Close':<8} | {'High40':<8} | {'Low40':<8} | {'Step':<4} | {'ActionIdx':<9} | {'TgtLev'}")
for i in range(len(df)):
    current_close = df['Close'].iloc[i]
    sma_fast = df['SMA_20'].iloc[i]
    sma_slow = df['SMA_200'].iloc[i]
    high_bw = df['High_40'].iloc[i]
    low_bw = df['Low_40'].iloc[i]
    
    if sma_fast < sma_slow or current_close < sma_slow:
        in_trend = False
        step_idx = 0
        target_history.append(base_leverage)
        continue
        
    if sma_fast >= sma_slow and not in_trend:
        in_trend = True
        
    if in_trend and (i - last_action_idx >= cooldown):
        if current_close > high_bw:
            if step_idx < max_idx:
                step_idx += 1
                last_action_idx = i
        elif current_close < low_bw:
            if step_idx > 0:
                step_idx -= 1
                last_action_idx = i
                
    alloc_pct = allocs[step_idx]
    target_lev = base_leverage * (1 - alloc_pct) + max_leverage * alloc_pct
    target_history.append(target_lev)
    
    if i >= len(df) - 20:
        print(f"{df.index[i].strftime('%Y-%m-%d')} | {current_close:<8.2f} | {high_bw:<8.2f} | {low_bw:<8.2f} | {step_idx:<4} | {last_action_idx:<9} | {target_lev:.2f}")

