const test = require('node:test');
const assert = require('node:assert/strict');
const Q = require('../variational_grid/web/qqq.js');
const M = require('../variational_grid/web/model.js');

test('percentages are percent units, preserve signs and distinguish missing from zero', () => {
  assert.equal(Q.percent(2), '2.00%');
  assert.equal(Q.percent(-2), '-2.00%');
  assert.equal(Q.percent(0), '0.00%');
  assert.equal(Q.percent(null), '—');
  assert.equal(Q.percent(undefined), '—');
});

test('nine combinations have stable spacing colors and hedge line patterns', () => {
  const encodings = [.05,.1,.2].flatMap(grid => [0,2,5].map(hedge => Q.encoding({grid_step_percent:String(grid),hedge_tolerance_percent:String(hedge)})));
  assert.equal(new Set(encodings.map(e => `${e.color}/${e.dash}`)).size, 9);
  assert.equal(Q.label({grid_step_percent:'.05',hedge_tolerance_percent:0}), '网格 0.05% / 对冲 0%');
});

test('net leg PnL reconciles to total without subtracting already charged fees again', () => {
  const row = {qqq:{total_pnl_usdc:'20'},us100:{total_pnl_usdc:'-8'},total_pnl_usdc:'12',fees_usdc:'1'};
  assert.equal(Q.reconciliation(row), true);
  assert.equal(Q.reconciliation({...row,total_pnl_usdc:'11'}), false);
  assert.equal(Q.reconciliation({...row,qqq:{}}), null);
});

test('signed exposure chart contains both threshold boundaries and actual residual', () => {
  const flat = Q.exposureDomain([0,null],0);
  assert.ok(flat[0] < 0 && flat[1] > 0);
  const range = Q.exposureDomain([-8,2],5);
  assert.ok(range[0] < -8 && range[1] > 8);
  assert.deepEqual(Q.exposureDomain([100],2),[-100,100]);
});

test('history looks up account names independently of scenario order and never turns missing into zero', () => {
  const history = {names:['b','a']}, point = {pnl:[12,-3],exposure:[null,0]};
  assert.equal(Q.historyValue(history,point,'a','pnl'),-3);
  assert.equal(Q.historyValue(history,point,'a','exposure'),0);
  assert.equal(Q.historyValue(history,point,'b','exposure'),null);
  assert.equal(Q.historyValue(history,point,'c','pnl'),null);
});

test('sampling pins timestamps and paths split on gaps or absent values', () => {
  const points = [{ts:10,segment:0,pnl:[0]},{ts:20,segment:0,pnl:[null]},{ts:30,segment:1,pnl:[2]},{ts:40,segment:1,pnl:[3]}];
  assert.equal(Q.sampleIndex(points,null),3);
  assert.equal(Q.sampleIndex(points,21),1);
  assert.equal(Q.sampleIndex([],20),-1);
  const path = M.path(points,p=>Q.historyValue({names:['a']},p,'a','pnl'),x=>x,y=>y);
  assert.equal((path.match(/M/g)||[]).length,2);
  assert.equal((path.match(/L/g)||[]).length,1);
  assert.ok(!path.includes('NaN'));
});

test('freshness uses server sample age plus elapsed browser time and preserves offline distinction', () => {
  const data = {summary:{poll_seconds:5},runtime:{status:'running'}};
  assert.equal(Q.freshness(data,0,1000,2000,false).label,'模拟运行中');
  assert.equal(Q.freshness(data,50,1000,12000,false).label,'行情已过期');
  assert.equal(Q.freshness(data,50,1000,12000,true).label,'页面连接中断');
  assert.equal(Q.freshness({...data,runtime:{status:'paused'}},0,1000,2000,false).label,'行情暂停');
});

test('market source availability is a last-observation state, never a claim of current freshness', () => {
  const data = {summary:{poll_seconds:5,market:{source_status:'ready'}},runtime:{status:'running'}};
  assert.equal(Q.marketStatus(data,Q.freshness(data,0,1000,2000,false),false),'末次行情可用');
  assert.equal(Q.marketStatus(data,Q.freshness(data,50,1000,12000,false),false),'已过期 · 末次行情可用');
  const stopped = {...data,runtime:{status:'stopped'}};
  assert.equal(Q.marketStatus(stopped,Q.freshness(stopped,0,1000,2000,false),false),'已停止 · 末次行情可用');
  assert.equal(Q.marketStatus(stopped,Q.freshness(stopped,50,1000,12000,false),false),'已过期 · 末次行情可用');
  assert.equal(Q.marketStatus(data,Q.freshness(data,0,1000,2000,true),true),'连接中断 · 末次行情可用');
  assert.equal(Q.marketStatus({summary:null},{stale:false},false),'等待行情');
});

test('hedge state distinguishes precision tails, unavailable quotes and market restrictions', () => {
  assert.equal(Q.hedgeStatus({hedge_pending:true,hedge_status:'quantity_rounding_residual',net_exposure_usdc:'.01'}),'数量精度尾差 · 保留实际敞口');
  assert.equal(Q.hedgeStatus({hedge_pending:true,hedge_status:'inside_band'}),'数量精度尾差 · 保留实际敞口');
  assert.equal(Q.hedgeStatus({hedge_pending:true,hedge_status:'var_close_only'}),'待对冲 · US100 仅允许减仓');
  assert.equal(Q.hedgeStatus({hedge_pending:true,hedge_status:'var_market_unavailable'}),'待对冲 · US100 行情不可用');
  assert.equal(Q.hedgeStatus({hedge_pending:true,hedge_status:'hedge_quote_unavailable_or_below_minimum'}),'待对冲 · 报价不可用或未达最小对冲量');
  assert.equal(Q.hedgeStatus({hedge_pending:false,hedge_status:'inside_band'}),'敞口在容忍范围内');
  assert.equal(Q.hedgeStatus({hedge_pending:false,hedge_status:'hedged'}),'已完成对冲');
});

test('separate minimum-size and missing-quote reasons stay distinct when published', () => {
  assert.equal(Q.hedgeStatus({hedge_pending:true,hedge_status:'hedge_below_minimum'}),'未达最小对冲量 · 保留实际敞口');
  assert.equal(Q.hedgeStatus({hedge_pending:true,hedge_status:'hedge_quote_unavailable'}),'待对冲 · 对冲报价不可用');
  assert.equal(Q.hedgeStatus({hedge_pending:true}),'待对冲 · 原因未提供');
});

test('account, range and detail views round-trip through existing URL contract', () => {
  assert.deepEqual(M.state('?strategy=qqq-005-h2&range=7d&view=trades',['qqq-005-h2']),{strategy:'qqq-005-h2',range:'7d',view:'trades'});
  assert.deepEqual(M.state('?strategy=bad&range=forever&view=secret',['qqq-005-h2']),{strategy:'all',range:'24h',view:'positions'});
});

test('rate limit countdown uses server time and elapsed time, never claims recovery at zero', () => {
  const data = {server_ts:100,rate_limits:[{venue:'Variational',retry_at:160},{venue:'Lighter',retry_at:190}]};
  assert.equal(Q.cooldownNotice(data,10),'Variational HTTP 429 限流：50 秒后重试；Lighter HTTP 429 限流：80 秒后重试');
  assert.match(Q.cooldownNotice(data,61),/Variational HTTP 429 限流：冷却结束，等待下一次行情结果/);
  assert.equal(Q.cooldownNotice({server_ts:100},0),'');
  assert.equal(Q.cooldownNotice({server_ts:100,rate_limits:[{venue:'invalid',retry_at:1000}]},0),'');
});

test('shared reference status preserves source time and distinguishes usable cache from expiry', () => {
  const data = {server_ts:120,summary:{market:{quote_cache:{mode:'shared_indicative_v1',source_ts:100,max_age_seconds:60,available:true,cache_used:true,refresh_error:'429'}}}};
  const active = Q.referenceStatus(data,5);
  assert.equal(active.age,25);
  assert.equal(active.usable,true);
  assert.match(active.label,/缓存估算/);
  assert.equal(Q.referenceStatus(data,41).usable,false);
  data.summary.market.quote_cache.available = false;
  assert.equal(Q.referenceStatus(data,0).usable,false);
  assert.equal(Q.referenceStatus({summary:{market:{}}},0),null);
});

test('dollar thresholds and history retain USDC units without percent chart clipping', () => {
  const row = {grid_step_percent:'.1',hedge_threshold_usdc:'3000',hedge_tolerance_percent:null};
  assert.equal(Q.dollarHedge(row),true);
  assert.match(Q.label(row),/3,000 USDC/);
  const domain = Q.dollarExposureDomain([-4500,1000],3000);
  assert.ok(domain[0] < -4500 && domain[1] > 4500);
  assert.equal(Q.historyValue({names:['a']},{net_exposure:[1500],exposure:[2]},'a','net_exposure'),1500);
  assert.equal(Q.dollarHedge({hedge_tolerance_percent:2}),false);
});

test('paper fill provenance distinguishes old exact quotes from fixed half spread estimates', () => {
  assert.equal(Q.fillPricing({venue:'Variational'}),'原精确数量报价');
  const label = Q.fillPricing({pricing_mode:'shared_indicative_v1',cache_used:true,quote_age_seconds:4,source_qty:'.01',half_spread_percent:'.0015'});
  assert.match(label,/缓存参考价估算/);
  assert.match(label,/半点差 0.0015%/);
});

test('only the published scalper model switches account and position semantics', () => {
  const legacy = {grid_step_percent:'.05',hedge_threshold_usdc:'3000'};
  assert.equal(Q.isScalper(legacy),false);
  assert.equal(Q.scalperStatus(legacy),null);
  assert.equal(Q.isScalper({scalper:{model:'future_model'}}),false);
  assert.match(Q.label(legacy),/^网格 0.05%/);
  const current = {...legacy,scalper:{model:'perp_dex_scalper_v1'}};
  assert.equal(Q.isScalper(current),true);
  assert.match(Q.label(current),/^剥头皮 0.05% \/ 对冲 3,000 USDC$/);
});

test('scalper state keeps fractional seconds, percent and price units without browser countdown', () => {
  const row = {scalper:{model:'perp_dex_scalper_v1',phase:'cooling_down',cooldown_remaining_seconds:112.5,grid_allowed:false,active_entries:0,active_take_profits:4,occupied_batches:4,max_batches:30,candidate_entry_price:'590.01',candidate_tp_price:'590.305'}};
  const status = Q.scalperStatus(row);
  assert.equal(status.phase,'等待开仓冷却');
  assert.equal(status.waiting,'采样时冷却剩余 112.5 秒');
  assert.equal(status.gate,'价格距离未满足');
  assert.equal(status.orders,'开仓 0 / 1 · TP 4 · 占用 4 / 30 批');
  assert.equal(status.prices,'候选开仓 590.0100 / TP 590.3050 USDC');
  assert.equal(Q.percent('.05'),'0.05%');
  assert.equal(Q.seconds(112.5),'112.5 秒');
  assert.equal(Q.seconds(null),'—');
  assert.deepEqual(Q.scalperStatus(row),status);
});

test('zero cooldown does not override blocked, paused or unassessed entry conditions', () => {
  const row = {scalper:{model:'perp_dex_scalper_v1',phase:'grid_blocked',cooldown_remaining_seconds:0,grid_allowed:false}};
  assert.equal(Q.scalperStatus(row).phase,'价格距离不足');
  assert.equal(Q.scalperStatus(row).waiting,'采样时冷却剩余 0 秒');
  assert.equal(Q.scalperStatus(row).pending,true);
  row.scalper.phase = 'entry_paused';
  assert.equal(Q.scalperStatus(row).phase,'行情受限，暂停开仓');
  row.scalper.grid_allowed = null;
  assert.equal(Q.scalperStatus(row).gate,'价格距离未评估');
  row.scalper.grid_allowed = true;
  assert.equal(Q.scalperStatus(row).gate,'价格距离已满足');
});

test('each published entry phase explains the latest observation, including absent fields', () => {
  const phases = {market_gap:'QQQ 行情过期或存在缺口，暂停开仓',awaiting_fill:'开仓单等待成交',cancel_pending:'等待撤单确认',capacity_full:'批次已满，暂停开仓',post_only_wait:'等待可挂 Maker 的价格',opening:'已提交模拟开仓单',take_profit_pending:'等待挂出独立止盈单'};
  for (const [phase,label] of Object.entries(phases)) assert.equal(Q.scalperStatus({scalper:{model:'perp_dex_scalper_v1',phase}}).phase,label);
  const unknown = Q.scalperStatus({scalper:{model:'perp_dex_scalper_v1'}});
  assert.equal(unknown.phase,'等待开仓状态');
  assert.equal(unknown.waiting,'采样时冷却剩余未提供');
  assert.equal(unknown.gate,'价格距离未评估');
  assert.equal(unknown.orders,'开仓 — / 1 · TP — · 占用 — / — 批');
  assert.equal(unknown.prices,'候选开仓 — / TP — USDC');
});

test('fill provenance follows each fill model and retains legacy reasons', () => {
  const entry = {venue:'Lighter',reason:'maker_entry',maker_model:'perp_dex_scalper_v1'};
  assert.equal(Q.fillReason(entry),'Maker 剥头皮开仓');
  assert.equal(Q.fillReason({...entry,reason:'maker_take_profit'}),'Maker 批次止盈');
  assert.equal(Q.fillPricing(entry),'剥头皮 · 严格 Maker 队列模拟');
  assert.equal(Q.fillReason({reason:'maker_entry'}),'Maker 网格买入');
  assert.equal(Q.fillReason({reason:'maker_take_profit'}),'Maker 网格止盈');
  assert.equal(Q.fillReason({...entry,reason:'delta_hedge'}),'敞口对冲调整');
  assert.equal(Q.fillReason({reason:'unrecognized'}),'unrecognized');
});

test('a waived cooldown or an active entry never displays a contradictory entry timer', () => {
  const row = {scalper:{model:'perp_dex_scalper_v1',phase:'opening',cooldown_remaining_seconds:100,cooldown_waived:true,active_entries:1}};
  assert.equal(Q.scalperStatus(row).waiting,'本轮批次减少，已跳过冷却');
  row.scalper.cooldown_waived = false;
  assert.equal(Q.scalperStatus(row).waiting,'开仓单处理中，下次冷却从入场完成起算');
});

function progressRow(overrides = {}) {
  return {scalper:{model:'perp_dex_scalper_v1', phase:'cooling_down', active_entries:0,
    cooldown_seconds:225, cooldown_remaining_seconds:112.5, next_entry_at:1112.5, ...overrides}};
}
function progressData(overrides = {}) {
  return {server_ts:1000, runtime:{status:'running'}, summary:{ts:1000, poll_seconds:2,
    market:{source_status:'ready', gap:false}}, ...overrides};
}

test('entry progress uses each published tier and both server sample age and monotonic elapsed time', () => {
  for (const total of [112.5,225,450,900]) {
    const p = Q.entryProgress(progressRow({cooldown_seconds:total,cooldown_remaining_seconds:total / 2}), progressData());
    assert.equal(p.percent,50);
    assert.equal(p.total,total);
    assert.equal(p.remaining,total / 2);
  }
  const p = Q.entryProgress(progressRow(),progressData({server_ts:1002}),3);
  assert.equal(p.remaining,107.5);
  assert.equal(p.percent,52.2);
  assert.equal(p.frozen,false);
  assert.match(p.basis,/估算/);
});

test('entry progress clamps boundaries without prematurely announcing completion', () => {
  assert.equal(Q.entryProgress(progressRow({cooldown_remaining_seconds:500}),progressData()).percent,0);
  const almost = Q.entryProgress(progressRow({cooldown_remaining_seconds:0.0001}),progressData());
  assert.equal(almost.percent,99.9);
  assert.match(almost.detail,/剩余 0.1 秒/);
  const done = Q.entryProgress(progressRow({cooldown_remaining_seconds:2}),progressData(),3);
  assert.equal(done.percent,100);
  assert.equal(done.remaining,0);
  assert.match(done.detail,/等待下一次采样检查/);
  assert.equal(Q.entryProgress(progressRow({cooldown_seconds:0,cooldown_remaining_seconds:0}),progressData()).percent,100);
});

test('an in-flight or cancelling entry never reuses the preceding cooldown or waiver', () => {
  for (const phase of ['opening','awaiting_fill','cancel_pending']) {
    const p = Q.entryProgress(progressRow({phase,cooldown_waived:true}),progressData(),10);
    assert.equal(p.mode,'order');
    assert.equal(p.percent,null);
    assert.equal(p.remaining,null);
    assert.match(p.detail,/成交或撤单确认后/);
  }
  assert.equal(Q.entryProgress(progressRow({active_entries:1}),progressData()).mode,'order');
});

test('first entry and waived wait are distinct from a timed cooldown and retain conditional wording', () => {
  const first = Q.entryProgress(progressRow({next_entry_at:null,cooldown_remaining_seconds:0}),progressData());
  assert.equal(first.mode,'first');
  assert.match(first.detail,/检查开仓条件/);
  const waived = Q.entryProgress(progressRow({cooldown_waived:true}),progressData());
  assert.equal(waived.mode,'waived');
  assert.match(waived.detail,/仍需满足/);
  assert.equal(waived.remaining,null);
});

test('missing or malformed cooldowns remain unknown and legacy grids have no progress', () => {
  assert.equal(Q.entryProgress({grid_step_percent:'0.05'},progressData()),null);
  for (const bad of [null,undefined,'',true,'bad',Infinity]) {
    for (const field of ['cooldown_seconds','cooldown_remaining_seconds']) {
      const p = Q.entryProgress(progressRow({[field]:bad}),progressData());
      assert.equal(p.mode,'unknown');
      assert.equal(p.percent,null);
    }
  }
  assert.equal(Q.entryProgress(progressRow({cooldown_seconds:-1}),progressData()).mode,'unknown');
  assert.equal(Q.entryProgress(progressRow({cooldown_seconds:0}),progressData()).mode,'unknown');
});

test('unavailable runtime, connection or server clock restores the explicit sample value', () => {
  for (const runtime of ['stopped','paused','degraded','starting','resetting',undefined]) {
    const p = Q.entryProgress(progressRow(),progressData({runtime:{status:runtime}}),20);
    assert.equal(p.frozen,true);
    assert.equal(p.remaining,112.5);
    assert.match(p.basis,/采样值/);
  }
  for (const server_ts of [null,undefined,'bad',999]) {
    assert.equal(Q.entryProgress(progressRow(),progressData({server_ts}),20).remaining,112.5);
  }
  const offline = Q.entryProgress(progressRow(),progressData(),20,true);
  assert.equal(offline.remaining,112.5);
  assert.match(offline.basis,/连接中断/);
  assert.equal(Q.entryProgress(progressRow(),progressData(),61).remaining,112.5);
  assert.equal(Q.entryProgress(progressRow(),progressData(),-10).remaining,112.5);
});

test('market gaps, unknown sources and expired Var references stop interpolation', () => {
  const d = progressData();
  for (const source_status of ['paused_entries','stale',undefined,'unknown']) {
    d.summary.market.source_status = source_status;
    assert.equal(Q.entryProgress(progressRow(),d,10).frozen,true);
  }
  d.summary.market = {source_status:'ready',gap:true};
  assert.equal(Q.entryProgress(progressRow(),d,10).frozen,true);
  d.summary.market.gap = false;
  for (const phase of ['market_gap','entry_paused']) assert.equal(Q.entryProgress(progressRow({phase}),d,10).frozen,true);
  d.summary.market.quote_cache = {mode:'shared_indicative_v1',available:true,source_ts:945,max_age_seconds:60};
  assert.equal(Q.entryProgress(progressRow(),d,5).frozen,false);
  const expired = Q.entryProgress(progressRow(),d,6);
  assert.equal(expired.frozen,true);
  assert.equal(expired.remaining,112.5);
  assert.match(expired.basis,/参考报价不可用/);
});

test('full cooldown never overrides price, capacity or maker eligibility gates', () => {
  for (const phase of ['grid_blocked','capacity_full','post_only_wait','take_profit_pending']) {
    const row = progressRow({phase,cooldown_remaining_seconds:0,grid_allowed:false});
    const p = Q.entryProgress(row,progressData());
    assert.equal(p.percent,100);
    assert.match(p.detail,/检查开仓条件/);
    assert.equal(Q.scalperStatus(row).gate,'价格距离未满足');
    assert.equal(Q.scalperStatus(row).pending,true);
  }
});

test('distance-free scalper labels compare TP targets and preserve historical model semantics', () => {
  const row = {grid_step_percent:'0.05',hedge_threshold_usdc:'3000',scalper:{model:'perp_dex_scalper_v2',take_profit_percent:'0.2',grid_allowed:null}};
  assert.equal(Q.isScalper(row),true);
  assert.equal(Q.distanceFree(row),true);
  assert.equal(Q.strategyTitle(row),'止盈 0.20%');
  assert.equal(Q.label(row),'剥头皮 TP 0.20% / 对冲 3,000 USDC');
  assert.equal(Q.scalperStatus(row).gate,'新开仓距离门槛已取消');
  assert.match(Q.entryDistanceRule(row),/^已取消/);
  row.scalper.model = 'perp_dex_scalper_v1';
  row.scalper.grid_allowed = false;
  assert.equal(Q.strategyTitle(row),'间距 0.05% / TP 0.20%');
  assert.equal(Q.scalperStatus(row).gate,'价格距离未满足');
  assert.match(Q.entryDistanceRule(row),/最小已有 TP/);
  assert.equal(Q.isScalper({scalper:{model:'unknown'}}),false);
  assert.equal(Q.strategyTitle({grid_step_percent:'0.1'}),'网格 0.10%');
});

test('distance-free model still publishes cooldown progress and labels fill provenance', () => {
  const row = progressRow({model:'perp_dex_scalper_v2',entry_distance_enabled:false,grid_allowed:null});
  assert.equal(Q.entryProgress(row,progressData()).percent,50);
  row.scalper.cooldown_remaining_seconds = 0;
  assert.match(Q.entryProgress(row,progressData()).detail,/检查开仓条件/);
  for (const model of ['perp_dex_scalper_v1','perp_dex_scalper_v2']) {
    assert.equal(Q.fillReason({maker_model:model,reason:'maker_entry'}),'Maker 剥头皮开仓');
    assert.equal(Q.fillReason({maker_model:model,reason:'maker_take_profit'}),'Maker 批次止盈');
    assert.equal(Q.fillPricing({maker_model:model,venue:'Lighter'}),'剥头皮 · 严格 Maker 队列模拟');
  }
});

test('reference authentication state is explicit and legacy snapshots keep their source label', () => {
  const quote_cache = {mode:'shared_indicative_v1',source_ts:100,max_age_seconds:60,available:true,authentication:'vr-token',authenticated:true};
  const data = {server_ts:102,summary:{market:{quote_cache}}};
  assert.match(Q.referenceStatus(data).label,/Var token 已验证/);
  quote_cache.available = false;
  quote_cache.authenticated = false;
  quote_cache.refresh_error = 'Var 会话被拒绝（HTTP 401/403）';
  assert.equal(Q.referenceStatus(data).usable,false);
  assert.match(Q.referenceStatus(data).error,/401\/403/);
  assert.match(Q.referenceStatus(data).label,/Var token 未就绪/);
  delete quote_cache.authentication;
  assert.doesNotMatch(Q.referenceStatus(data).label,/Var token/);
});

test('GTT take-profit model displays execution provenance and exact uncovered reasons', () => {
  const row = {grid_step_percent:'0.05',scalper:{model:'perp_dex_scalper_v3',phase:'take_profit_pending',take_profit_percent:'0.05',take_profit_blockers:[{slot:2,quantity:'0.01',reason:'below_min_notional'}]}};
  assert.equal(Q.isScalper(row),true);
  assert.equal(Q.distanceFree(row),true);
  assert.equal(Q.strategyTitle(row),'止盈 0.05%');
  assert.match(Q.scalperStatus(row).phase,/止盈单未挂出/);
  assert.match(Q.scalperStatus(row).exitDetail,/批次 2：未达最小下单金额/);
  assert.match(Q.scalperStatus(row).exitDetail,/0\.010000 QQQ/);
  row.scalper.take_profit_blockers = [];
  row.scalper.take_profits_in_flight = 1;
  assert.match(Q.scalperStatus(row).exitDetail,/1 笔止盈单.*新盘口/);
  assert.equal(Q.fillReason({maker_model:'perp_dex_scalper_v3',reason:'taker_take_profit'}),'Taker 批次止盈');
  assert.match(Q.fillPricing({maker_model:'perp_dex_scalper_v3',reason:'taker_take_profit',quote_source_ts:100}),/GTT 限价止盈/);
  assert.equal(Q.fillReason({maker_model:'perp_dex_scalper_v3',reason:'maker_take_profit'}),'Maker 批次止盈');
});

test('saving a token does not relabel an older quote as the new session confirmation', () => {
  const cache = {mode:'shared_indicative_v1',source_ts:100,max_age_seconds:60,available:true,authentication:'vr-token',authenticated:true};
  const data = {server_ts:106,var_session:{updated_ts:102},summary:{market:{quote_cache:cache}}};
  assert.match(Q.referenceStatus(data).label,/等待新 token 报价/);
  assert.equal(Q.referenceStatus(data).usable,false);
  cache.source_ts = 105;
  assert.equal(Q.referenceStatus(data).usable,true);
  assert.match(Q.referenceStatus(data).label,/Var token 已验证/);
});

test('dust IOC is a priced exit attempt and does not masquerade as blocked entries', () => {
  const row = {scalper:{model:'perp_dex_scalper_v3',phase:'opening',take_profit_blockers:[],small_take_profits:[{slot:56,quantity:'0.0008',limit:'743.4'}]}};
  const status = Q.scalperStatus(row);
  assert.equal(status.phase,'已提交模拟开仓单');
  assert.equal(status.pending,false);
  assert.match(status.exitDetail,/批次 56：小额余仓 0\.000800 QQQ/);
  assert.match(status.exitDetail,/不低于 743\.4000 USDC 的限价 IOC/);
  assert.match(status.exitDetail,/不阻塞其他开仓/);
  assert.doesNotMatch(status.exitDetail,/未覆盖|未达最小/);
  const fill = {maker_model:'perp_dex_scalper_v3',reason:'taker_take_profit',time_in_force:'IOC',quote_source_ts:100};
  assert.equal(Q.fillReason(fill),'IOC 小额止盈');
  assert.match(Q.fillPricing(fill),/IOC 小额限价止盈/);
  assert.doesNotMatch(Q.fillPricing(fill),/GTT/);
});
