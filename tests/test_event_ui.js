// 真原JS在最小DOM/HTTP替身執行；只測本輪事件呈現，不抓行情或通知。
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
    querySelector(s) { if (!this.parts.has(s)) this.parts.set(s, new Element()); return this.parts.get(s); }
    appendChild(x) { this.children.push(x); }
    addEventListener(k, fn) { this.listeners[k] = fn; }
}

function market(ticker, extra = {}) {
    return {id: ticker.replace('.', '_').toLowerCase(), name: ticker, ticker, date: '2026-11-04', close: 100,
        in_trend: true, step_idx: 3, max_steps: 4, target_today: 2.8, target_yesterday: 2.8,
        trim_trigger_lev: 2.95, sma_slow_val: 90, sma_fast_val: 99, sma_bear_val: 95, high_bw: 101, low_bw: 90,
        base_leverage: 1, max_leverage: 3, explanation: '事件測試', last_action: null, last_trim: null, ...extra};
}

async function render(markets) {
    const ids = new Map(['dashboard', 'market-card-template', 'global-action-pill', 'data-date-tag', 'rollover-cards'].map(x => [x, new Element()]));
    const cards = [];
    ids.get('market-card-template').content = {cloneNode: () => { const el = new Element(); cards.push(el); return el; }};
    let ready;
    const context = {
        document: {getElementById: k => ids.get(k), querySelector: () => null, createElement: () => new Element(), addEventListener: (_, cb) => { ready = cb; }},
        window: {location: {hostname: 'example.github.io', protocol: 'https:'}},
        localStorage: {getItem: () => null, setItem: () => {}},
        fetch: url => Promise.resolve({status: 200, json: () => Promise.resolve(url.includes('api_rollover') ? {} : markets)}),
        setTimeout: () => {}, Date, console
    };
    vm.runInNewContext(source, context, {filename: scriptPath}); ready();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(cards.length, 2);
    return {cards, global: ids.get('global-action-pill').textContent};
}

const evt = (type, from, to, days = 1) => ({date: days === 0 ? '2026-11-04' : '2026-11-03', from_lev: from, to_lev: to, type, days_ago: days});
const text = (r, i, selector) => r.cards[i].querySelector(selector).textContent;

(async () => {
    const trim = evt('+0.15x超標修剪', 3.04, 2.8);
    const reduction = {...evt('跌破通道減碼', 2.23, .8, 15), date: '2026-10-14'};
    const legacy = await render([
        market('QQQ', {target_today: .8, target_yesterday: .8, last_action: reduction, last_trim: reduction}),
        market('006208.TW', {last_action: trim, last_trim: trim})
    ]);
    assert.match(text(legacy, 0, '.last-action-val'), /跌破通道減碼/);
    assert.doesNotMatch(text(legacy, 0, '.last-trim-val'), /跌破通道減碼/);
    assert.match(text(legacy, 1, '.last-action-val'), /舊版未分列/);
    assert.match(text(legacy, 1, '.last-trim-val'), /3\.04x➔2\.80x/);
    assert.match(text(legacy, 1, '.delta-badge'), /^維持 2\.80x$/);
    assert.doesNotMatch(legacy.global, /變動|修剪/);

    const signal = {...evt('跌破通道減碼', 3, 2.8, 5), date: '2026-10-28'};
    const split = await render([market('QQQ'), market('006208.TW', {
        last_signal_change: signal, last_excess_trim: trim, last_action: trim, last_trim: trim
    })]);
    assert.match(text(split, 1, '.last-action-val'), /2026-10-28/);
    assert.doesNotMatch(text(split, 1, '.last-action-val'), /3\.04/);
    assert.match(text(split, 1, '.last-trim-val'), /2026-11-03/);

    const explicitNull = await render([market('QQQ'), market('006208.TW', {
        last_signal_change: null, last_excess_trim: null, last_action: trim, last_trim: trim
    })]);
    assert.equal(text(explicitNull, 1, '.last-action-val'), '尚無獨立紀錄');
    assert.equal(text(explicitNull, 1, '.last-trim-val'), '尚無獨立紀錄');

    const currentTrim = evt('+0.15x超標修剪', 3.04, 2.8, 0);
    const now = await render([market('QQQ'), market('006208.TW', {last_signal_change: signal, last_excess_trim: currentTrim})]);
    assert.match(text(now, 1, '.delta-badge'), /本期模擬修剪（超標/);
    assert.match(now.global, /模擬超標修剪/);

    const precision = await render([market('QQQ'), market('006208.TW', {target_today: 2.8000000000000003, target_yesterday: 2.8})]);
    assert.equal(text(precision, 1, '.delta-badge'), '維持 2.80x');
    assert.doesNotMatch(precision.global, /變動/);

    const template = fs.readFileSync(path.resolve(__dirname, '..', 'templates', 'index.html'), 'utf8');
    assert.match(template, /最近目標變更/); assert.match(template, /最近模擬超標修剪/); assert.match(template, /非實帳/);
    console.log(JSON.stringify({passed: true, cases: ['legacy_public_snapshot_shape', 'split_history', 'explicit_null_no_fallback', 'current_excess_trim', 'unchanged_rounded_2_8', 'template_labels']}));
})().catch(error => { console.error(error); process.exitCode = 1; });
