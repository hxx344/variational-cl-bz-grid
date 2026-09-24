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
