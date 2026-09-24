const test = require('node:test');
const assert = require('node:assert/strict');
const M = require('../variational_grid/web/model.js');

test('percentage labels do not confuse effective dollar steps or legacy grids', () => {
  assert.equal(M.gridLabel({grid_step_percent:'0.5',grid_step:'0.02'}), '间距 0.5%');
  assert.equal(M.gridLabel({grid_step_percent:'1',grid_step:'0.04'}), '间距 1%');
  assert.equal(M.gridLabel({grid_step_percent:'2',grid_step:'0.08'}), '间距 2%');
  assert.equal(M.gridLabel({grid_step_percent:'0.25',grid_step:'0.01'}), '间距 0.25%');
  assert.equal(M.gridLabel({grid_step:'0.20'}), '间距 0.20 USDC/桶');
});

test('unknown values stay missing and signed losses are explicit', () => {
  for(const value of [null, undefined, '', NaN, Infinity]) assert.equal(M.number(value), '—');
  assert.equal(M.number(0), '0.0000');
  assert.equal(M.signed(-1.2), '-1.2000');
  assert.equal(M.signed(1.2), '+1.2000');
  assert.equal(M.tone(-1), 'loss');
});
test('range labels distinguish one direction from the full span', () => {
  assert.equal(M.rangeLabel({grid_range_percent:'30.0',grid_span_percent:'60.0'}), '上下各 30% · 总跨度 60%');
  assert.equal(M.rangeLabel({max_levels:8}), '每侧 8 层');
});

test('unlimited funding is explicit and distinct from zero or missing limits', () => {
  assert.equal(M.limitLabel({margin_limit_enabled:false,entry_notional_limit_usdc:null}), '金额不限');
  assert.equal(M.limitLabel({margin_limit_enabled:false,margin_limit_usdc:null}, 'margin_limit_usdc'), '金额不限');
  assert.equal(M.limitLabel({margin_limit_enabled:true,entry_notional_limit_usdc:'0'}), '0.00 USDC');
  assert.equal(M.limitLabel({entry_notional_limit_usdc:'4000'}), '4,000.00 USDC');
  assert.equal(M.limitLabel({entry_notional_limit_usdc:null}), '—');
  assert.equal(M.limitLabel({}), '—');
});
test('time is Beijing time, independent of the viewer timezone', () => {
  assert.match(M.date(1735689600), /01.01.*08:00:00/);
  assert.match(M.date(1735689600, false, true), /2025/);
});
test('URL state is repeatable and invalid filters fall back', () => {
  assert.deepEqual(M.state('?strategy=step-0.20&range=7d&view=trades', ['step-0.20']), {strategy:'step-0.20', range:'7d', view:'trades'});
  assert.deepEqual(M.state('?strategy=other&range=bad&view=bad', []), {strategy:'all', range:'24h', view:'positions'});
});
test('axes include zero for PnL and leave a nonzero domain for flat data', () => {
  const [lo, hi] = M.domain([1,2], true);
  assert.ok(lo<0 && hi>2);
  const [a,b] = M.domain([7,7]);
  assert.ok(a<7 && b>7);
  assert.deepEqual(M.domain([null, undefined]), [-1,1]);
});
test('paths break on missing samples and preserved outage segments', () => {
  const points = [1,2,null,4,5,6].map((v,i)=>({ts:i,v,segment:i<4?0:1}));
  const result = M.path(points, p=>p.v, x=>x, y=>y);
  assert.equal((result.match(/M/g)||[]).length,3);
  assert.ok(!result.includes('NaN'));
});
test('CSV keeps negative amounts numeric and neutralizes spreadsheet formulas', () => {
  const text = M.csv([['中文','=1+1','+SUM(1)', '-1.20', 'a"b', 'a,b']]);
  assert.ok(text.startsWith('\ufeff'));
  assert.ok(text.includes('"\'=1+1"'));
  assert.ok(text.includes('"\'+SUM(1)"'));
  assert.ok(text.includes('"-1.20"'));
  assert.ok(text.includes('"a""b"'));
  assert.equal(M.escape('<script>"'), '&lt;script&gt;&quot;');
});
