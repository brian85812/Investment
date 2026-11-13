// 執行真實 script.js；DOM、HTTP 與計時器皆為離線替身。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const scriptPath = path.resolve(__dirname, '..', 'static', 'script.js');
const source = fs.readFileSync(scriptPath, 'utf8');

class Element {
    constructor() {
        this.parts = new Map(); this.children = []; this.listeners = {};
        this.textContent = ''; this.innerHTML = ''; this.value = ''; this.className = ''; this.open = false;
        const classes = new Set(['hidden']);
        this.classList = {add: (...xs) => xs.forEach(x => classes.add(x)), remove: (...xs) => xs.forEach(x => classes.delete(x)), contains: x => classes.has(x)};
    }
    querySelector(selector) { if (!this.parts.has(selector)) this.parts.set(selector, new Element()); return this.parts.get(selector); }
    appendChild(child) { this.children.push(child); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
}

function state(extra = {}) {
    return {schema_version: 1, asof_date: '2026-11-06', stage: 'bull', label: '多頭階梯', flags: [],
        cooldown: {active: false, timer_active: false, applies_to_current_stage: true, total_days: 10, elapsed_days: 10, remaining_days: 0},
        grace: {active: false}, blocked_actions: [], d1: {blocked: false}, extension: {capping_active: false},
        bull_acceleration: {active: false}, priority_defense: {active: false, bypasses_step_cooldown: true}, ...extra};
}

function market(ticker, extra = {}) {
    return {id: ticker.replace('.', '_').toLowerCase(), name: ticker, ticker, date: '2026-11-06', expected_session: '2026-11-06',
        is_stale: false, fallback_used: false, data_status: 'ok', close: 100, in_trend: true,
        step_idx: 3, max_steps: 4, target_today: 2.8, target_yesterday: 2.8, trim_trigger_lev: 2.95,
        sma_slow_val: 90, sma_fast_val: 99, sma_bear_val: 95, high_bw: 101, low_bw: 90,
        base_leverage: 1, max_leverage: 3, explanation: '不可盲用的舊通用摘要：趨勢穩健，無操作',
        last_signal_change: null, last_excess_trim: null, strategy_state: state(), ...extra};
}

async function render(markets) {
    const ids = new Map(['dashboard', 'market-card-template', 'global-action-pill', 'data-date-tag', 'rollover-cards'].map(id => [id, new Element()]));
    const cards = [];
    ids.get('market-card-template').content = {cloneNode: () => { const card = new Element(); cards.push(card); return card; }};
    let ready;
    vm.runInNewContext(source, {
        document: {getElementById: id => ids.get(id), querySelector: () => null, createElement: () => new Element(), addEventListener: (_, callback) => { ready = callback; }},
        window: {location: {hostname: 'example.github.io', protocol: 'https:'}},
        localStorage: {getItem: () => null, setItem: () => {}},
        fetch: url => Promise.resolve({status: 200, json: () => Promise.resolve(url.includes('api_rollover') ? {} : markets)}),
        setTimeout: () => {}, Date, console
    }, {filename: scriptPath});
    ready();
    await new Promise(resolve => setImmediate(resolve));
    return {cards, global: ids.get('global-action-pill'), dates: ids.get('data-date-tag')};
}

const text = (result, index, selector) => result.cards[index].querySelector(selector).textContent;
const event = (kind, date = '2026-11-05') => ({date, days_ago: 0, from_lev: 3, to_lev: 2.8,
    type: kind === 'excess_trim' ? '+0.15x超標修剪' : '跌破通道減碼'});

(async () => {
    const cases = [];
    const stale = await render([market('QQQ'), market('006208.TW', {date: '2026-11-05', is_stale: true,
        target_yesterday: 3, last_signal_change: event('signal_change'), last_excess_trim: event('excess_trim')})]);
    assert.match(stale.global.textContent, /台股資料延遲：11\/05，應11\/06/);
    assert.doesNotMatch(stale.global.textContent, /資料缺失|來源失敗|🟢/);
    assert.match(text(stale, 1, '.delta-badge'), /截至 2026-11-05.*待更新/);
    assert.doesNotMatch(text(stale, 1, '.delta-badge'), /目標變更|本期模擬修剪/);
    assert.match(text(stale, 1, '.last-action-val'), /2026-11-05.*跌破通道減碼/);
    assert.match(text(stale, 1, '.last-trim-val'), /2026-11-05.*超標修剪/);
    const card = stale.cards[1];
    card.querySelector('.equity-input').value = '100000';
    card.querySelector('.position-input').value = '300000';
    card.querySelector('.position-input').listeners.input();
    assert.match(text(stale, 1, '.action-instruction'), /暫不提供買賣指令/);
    assert.doesNotMatch(card.querySelector('.action-instruction').innerHTML, /請淨減持|請淨加買|單一淨指令/);
    assert.equal(card.querySelector('.equity-input').value, '100000');
    cases.push('stale_exact_reason_suppresses_current_alert_and_definite_order_keeps_history_and_inputs');

    const inferred = await render([market('QQQ'), market('006208.TW', {date: '2026-11-05'})]);
    assert.match(inferred.global.textContent, /台股資料延遲：11\/05，應11\/06/);
    cases.push('expected_session_date_detects_stale_even_if_flag_inconsistent');

    const unconfiguredBackup = await render([market('QQQ', {date: '2026-11-05',
        secondary_source: {provider: 'futu', status: 'authorization_required'}}), market('006208.TW')]);
    assert.match(unconfiguredBackup.global.textContent, /美股資料延遲.*此網站尚未設定富途備援憑證/);
    assert.match(text(unconfiguredBackup, 0, '.stale-badge'), /此網站尚未設定富途備援憑證/);
    assert.match(text(unconfiguredBackup, 0, '.calc-freshness-note'), /尚未設定富途.*暫停確定調倉試算/);
    assert.match(text(unconfiguredBackup, 0, '.delta-badge'), /待更新/);
    cases.push('stale_futu_missing_runtime_credentials_are_explicit_and_orders_remain_blocked');

    for (const [status, expected] of [['authorization_failed', /授權被拒絕/],
        ['adjusted_price_basis_mismatch', /還原價口徑不符.*拒絕補價/],
        ['provider_unavailable', /備援連線失敗/]]) {
        const problem = await render([market('QQQ', {date: '2026-11-05',
            secondary_source: {provider: 'futu', status}}), market('006208.TW')]);
        assert.match(text(problem, 0, '.stale-badge'), expected);
        assert.match(text(problem, 0, '.delta-badge'), /待更新/);
    }
    const unknownBackup = await render([market('QQQ', {date: '2026-11-05',
        secondary_source: {provider: 'futu', status: '<unexpected_private_provider_message>'}}), market('006208.TW')]);
    assert.doesNotMatch(unknownBackup.global.textContent, /unexpected_private_provider_message/);
    const repairedBackup = await render([market('QQQ', {
        secondary_source: {provider: 'futu', status: 'repaired'}}), market('006208.TW')]);
    assert.doesNotMatch(repairedBackup.global.textContent, /資料延遲|備援憑證|拒絕補價|連線失敗/);
    cases.push('futu_failure_causes_are_distinct_and_unknown_details_are_not_echoed');

    const fallback = await render([market('QQQ'), market('006208.TW', {fallback_used: true, target_yesterday: 3})]);
    assert.match(fallback.global.textContent, /台股來源失敗，使用備援資料/);
    assert.doesNotMatch(fallback.global.textContent, /資料延遲|資料缺失/);
    assert.match(text(fallback, 1, '.delta-badge'), /待更新/);
    cases.push('same_date_fallback_is_source_failure_not_date_delay');

    const cachedFallback = await render([market('QQQ'), market('006208.TW', {data_status: 'fallback_stale'})]);
    assert.match(cachedFallback.global.textContent, /來源失敗，使用備援資料/);
    assert.doesNotMatch(cachedFallback.global.textContent, /資料延遲/);
    assert.match(text(cachedFallback, 1, '.delta-badge'), /待更新/);
    const uncertainFreshness = await render([market('QQQ'), market('006208.TW', {is_stale: true})]);
    assert.match(uncertainFreshness.global.textContent, /新鮮度未確認.*應有日期待核/);
    assert.doesNotMatch(uncertainFreshness.global.textContent, /資料延遲：11\/06，應11\/06/);
    cases.push('legacy_stale_flag_never_fabricates_a_date_gap');

    const missing = await render([market('QQQ')]);
    assert.match(missing.global.textContent, /台股市場資料缺失/);
    assert.doesNotMatch(missing.global.textContent, /資料延遲/);
    cases.push('missing_market_is_named');

    const holiday = await render([market('QQQ', {date: '2026-11-05', expected_session: '2026-11-05'}), market('006208.TW')]);
    assert.doesNotMatch(holiday.global.textContent, /資料延遲|資料缺失/);
    assert.match(holiday.dates.textContent, /各市場交易日分列/);
    cases.push('different_fresh_market_dates_do_not_imply_delay');

    const cd = {active: true, timer_active: true, applies_to_current_stage: true, total_days: 10, elapsed_days: 1, remaining_days: 9,
        last_step_from: 3, last_step_to: 2.8};
    const cooling = await render([market('QQQ'), market('006208.TW', {strategy_state: state({cooldown: cd, flags: ['cooldown']})})]);
    for (const selector of ['.cooldown-badge', '.high-bw-val', '.low-bw-val', '.strategy-explanation']) {
        assert.match(text(cooling, 1, selector), /冷卻中.*餘 9 個市場交易日/);
    }
    assert.doesNotMatch(text(cooling, 1, '.strategy-explanation'), /趨勢穩健|無操作/);
    assert.match(text(cooling, 1, '.strategy-explanation'), /實帳超過目標 \+0\.15x 仍須另核/);
    assert.match(cooling.global.textContent, /台股減碼後階梯冷卻中.*餘 9 個市場交易日/);
    assert.equal(cooling.global.className, 'action-pill warn');
    cases.push('cooldown_remaining_visible_in_both_channels_and_summary');

    const concurrent = await render([market('QQQ'), market('006208.TW', {
        last_excess_trim: event('excess_trim', '2026-11-06'),
        strategy_state: state({cooldown: {...cd, elapsed_days: 5, remaining_days: 5}, flags: ['cooldown'], triggers: {breakout_down: true}})
    })]);
    assert.match(concurrent.global.textContent, /模擬超標修剪.*減碼後階梯冷卻中.*餘 5 個市場交易日/);
    assert.match(text(concurrent, 1, '.low-bw-val'), /減碼後階梯冷卻中.*資料日已跌破通道/);
    assert.match(text(concurrent, 1, '.strategy-explanation'), /冷卻中.*模擬超標修剪/);
    assert.doesNotMatch(concurrent.global.textContent, /🟢|資料缺失|資料延遲/);
    cases.push('fresh_cooldown_and_current_simulated_trim_are_both_visible');

    const grace = await render([market('QQQ'), market('006208.TW', {close: 89,
        strategy_state: state({flags: ['grace'], grace: {active: true, days_elapsed: 1, days_allowed: 1, days_remaining: 0, exit_after_consecutive_days: 2}})})]);
    assert.equal(text(grace, 1, '.status-badge'), '多頭狀態');
    assert.match(text(grace, 1, '.grace-badge'), /年線容忍 1\/1 日/);
    assert.match(text(grace, 1, '.strategy-explanation'), /連續 2 日才退出/);
    assert.doesNotMatch(text(grace, 1, '.status-badge'), /安全/);
    cases.push('annual_grace_is_not_called_safe');

    const defense = await render([market('QQQ'), market('006208.TW', {in_trend: false, step_idx: 0,
        strategy_state: state({stage: 'defense_deep', label: 'C1 深熊防禦', cooldown: {...cd, active: false, applies_to_current_stage: false}})})]);
    assert.match(text(defense, 1, '.cooldown-badge'), /防禦階段.*冷卻不適用/);
    assert.doesNotMatch(text(defense, 1, '.low-bw-val'), /冷卻中/);
    assert.match(text(defense, 1, '.strategy-explanation'), /C1 深熊防禦/);
    cases.push('priority_defense_not_falsely_locked_by_step_timer');

    const d1 = await render([market('QQQ', {sma_fast_val: 95, sma_bear_val: 95, strategy_state: state({cooldown: cd, d1: {blocked: true},
        blocked_actions: [{action: 'step_up', reason: 'd1_filter', label: 'D1 升檔條件未滿足'}]})}), market('006208.TW')]);
    assert.match(text(d1, 0, '.high-bw-val'), /D1.*冷卻中/);
    assert.match(text(d1, 0, '.strategy-explanation'), /D1 僅凍結升檔/);
    assert.match(text(d1, 0, '.high-bw-val'), /D1 凍結/); // 顯示值四捨五入相等，仍以後端 raw 判定為準。
    cases.push('d1_and_cooldown_coexist');

    const multi = await render([market('QQQ'), market('006208.TW', {strategy_state: state({label: 'Extension 封頂', flags: ['extension', 'bull_accel', 'full'],
        extension: {capping_active: true, cap: 1.8}, bull_acceleration: {active: true, days_including_data_bar: 17}})})]);
    assert.match(text(multi, 1, '.strategy-explanation'), /Extension 封頂 1\.8x.*牛初加速.*17.*階梯已滿載/);
    cases.push('coexisting_flags_are_not_lost_to_stage_priority');

    const accelLast = await render([market('QQQ'), market('006208.TW', {bull_accel_active: true, bull_accel_days_left: 0,
        strategy_state: state({stage: 'bull_accel', label: '牛初加速', bull_acceleration: {active: true, days_remaining_after_data_bar: 0, days_including_data_bar: 1}})})]);
    assert.match(text(accelLast, 1, '.accel-badge'), /含資料日餘 1 個市場交易日/);
    assert.doesNotMatch(text(accelLast, 1, '.accel-badge'), /餘 0/);
    cases.push('last_acceleration_day_is_one_including_data_bar_not_zero');

    const legacy = market('006208.TW'); delete legacy.strategy_state;
    const old = await render([market('QQQ'), legacy]);
    assert.match(text(old, 1, '.cooldown-badge'), /冷卻資訊未提供/);
    assert.match(text(old, 1, '.low-bw-val'), /冷卻資訊未提供/);
    assert.doesNotMatch(text(old, 1, '.cooldown-badge'), /餘 \d/);
    cases.push('legacy_never_invents_cooldown');

    const boundary = await render([market('QQQ'), market('006208.TW', {strategy_state: state({cooldown: {...cd, active: false, remaining_days: 0, elapsed_days: 10}})})]);
    assert.match(text(boundary, 1, '.cooldown-badge'), /冷卻已結束/);
    assert.doesNotMatch(text(boundary, 1, '.low-bw-val'), /冷卻中/);
    cases.push('cooldown_boundary_uses_backend_state');

    const annual = await render([market('QQQ', {in_trend: false, step_idx: 0, close: 690.38,
        target_today: .8, target_yesterday: .8, sma_fast_val: 669.53, sma_slow_val: 675.04,
        sma_bear_val: 717.78, fast_ma_len: 5, slow_ma_len: 220, bo_up_len: 8,
        strategy_state: state({stage: 'defense', label: '常態防禦底倉', d1: {enabled: true, blocked: false},
            priority_defense: {active: true, reasons: ['fast_below_annual'], bypasses_step_cooldown: true}})}), market('006208.TW')]);
    assert.match(text(annual, 0, '.high-bw-val'), /收盤 ≥ 675\.04 且 5MA ≥ 220MA/);
    assert.match(text(annual, 0, '.high-bw-val'), /5MA仍低於220MA/);
    assert.equal(text(annual, 0, '.high-bw-label'), '🧭 防禦轉多頭／E3b條件');
    assert.equal(text(annual, 1, '.high-bw-label'), '📈 階梯升檔門檻 (Close)');
    assert.match(text(annual, 0, '.strategy-explanation'), /進入多頭後.*突破 8 日通道.*D1/);
    assert.match(text(annual, 0, '.delta-badge'), /維持 0\.80x/);
    cases.push('price_above_annual_still_requires_fast_ma_and_separate_step_filters');

    const roundedAnnual = await render([market('QQQ', {in_trend: false, step_idx: 0, sma_fast_val: 100,
        sma_slow_val: 100, sma_bear_val: 110, strategy_state: state({stage: 'defense',
            priority_defense: {active: true, reasons: ['fast_below_annual']}})}), market('006208.TW')]);
    assert.match(text(roundedAnnual, 0, '.high-bw-val'), /5MA仍低於220MA/);
    cases.push('annual_fast_block_uses_backend_reason_when_rounded_values_are_equal');

    const e3bActive = await render([market('QQQ', {in_trend: false, pre_bull_active: true,
        strategy_state: state({stage: 'e3b', label: 'E3b季線總攻'})}), market('006208.TW')]);
    assert.match(text(e3bActive, 0, '.high-bw-val'), /收盤與快線守季線.*重返多頭須.*5MA ≥ 220MA/);
    cases.push('e3b_maintenance_and_bull_reentry_show_distinct_complete_requirements');

    const e3bEntry = await render([market('QQQ', {in_trend: false}), market('006208.TW')]);
    assert.match(text(e3bEntry, 0, '.high-bw-val'), /E3b 進場須收盤 > 101\.00.*重返多頭須/);
    cases.push('e3b_strict_breakout_and_annual_inclusive_reentry_are_explicit');

    assert.match(text(annual, 0, '.entry-trend-check'), /尚未恢復.*恢復多頭不代表直接加碼/);
    assert.match(text(annual, 0, '.entry-d1-check'), /恢復多頭後仍須核對.*5MA 669\.53.*60MA 717\.78/);
    assert.doesNotMatch(text(annual, 0, '.entry-d1-check'), /已通過/);
    assert.match(text(annual, 0, '.entry-breakout-check'), /前 8 日最高收盤/);
    cases.push('defense_shows_future_step_filters_without_claiming_inactive_d1_has_passed');

    const reentered = await render([market('QQQ', {step_idx: 0, max_steps: 3, close: 688.17,
        sma_fast_val: 677.75, sma_slow_val: 675.36, sma_bear_val: 717.33, high_bw: 691.95,
        bo_up_len: 8, target_today: .8, target_yesterday: .8,
        strategy_state: state({stage: 'd1_frozen', label: 'D1季線濾網限制升檔', d1: {enabled: true, blocked: true},
            triggers: {breakout_up: false}})}), market('006208.TW')]);
    assert.match(text(reentered, 0, '.entry-trend-check'), /已恢復.*不代表直接加碼/);
    assert.match(text(reentered, 0, '.entry-breakout-check'), /未突破.*688\.17.*691\.95/);
    assert.match(text(reentered, 0, '.entry-d1-check'), /未通過.*677\.75.*717\.33/);
    assert.match(text(reentered, 0, '.entry-cooldown-check'), /已完成/);
    assert.match(text(reentered, 0, '.high-bw-val'), /691\.95.*D1 凍結.*60MA 717\.33/);
    assert.match(text(reentered, 1, '.entry-d1-check'), /本市場不啟用/);
    assert.match(text(stale, 1, '.entry-check-note'), /資料待更新.*暫不提供本期調倉指示/);
    cases.push('reentry_without_breakout_or_d1_keeps_all_simultaneous_requirements_visible');

    assert.match(text(d1, 0, '.entry-d1-check'), /未通過/);
    assert.match(text(d1, 0, '.entry-cooldown-check'), /餘 9 個市場交易日/);
    assert.match(text(old, 1, '.entry-cooldown-check'), /判定未提供/);
    cases.push('entry_checklist_preserves_raw_backend_decisions_and_unknown_legacy_state');
    console.log(JSON.stringify({passed: true, cases, samples: {
        stale_top: stale.global.textContent,
        stale_delta: text(stale, 1, '.delta-badge'),
        cooldown_top: cooling.global.textContent,
        cooldown_sell_channel: text(cooling, 1, '.low-bw-val'),
        grace_summary: text(grace, 1, '.strategy-explanation'),
        fallback_top: fallback.global.textContent
    }}));
})().catch(error => { console.error(error); process.exitCode = 1; });
