const test = require('node:test');
const assert = require('node:assert/strict');
const I = require('../variational_grid/web/inventory.js');
const M = require('../variational_grid/web/model.js');

test('ratio labels distinguish missing, neutral, fractional and unlimited thresholds', () => {
  assert.equal(I.percent(null), '—');
  assert.equal(I.percent(0), '0.00%');
  assert.equal(I.tolerance({tolerance_percent:null}), '不限');
  assert.equal(I.tolerance({tolerance_percent:0}), '0%');
  assert.equal(I.tolerance({tolerance_percent:'0.5'}), '0.5%');
  assert.equal(I.tolerance({}), '—');
});

test('attribution subtracts executed and reserved costs exactly once', () => {
  const row = {direction_pnl_usdc:'-1.5', spread_pnl_usdc:'4.7', execution_cost_usdc:'0.2', exit_cost_reserve_usdc:'0.1', total_pnl_usdc:'2.9', fees_usdc:'0.12', hedge_cost_usdc:'0.15'};
  assert.equal(I.reconciliation(row).matches, true);
  assert.equal(I.reconciliation({...row,total_pnl_usdc:'2.78'}).matches, false);
  assert.equal(I.reconciliation({...row,exit_cost_reserve_usdc:null}).matches, null);
});

test('exposure needs an observed interval, while zero exposure is valid', () => {
  assert.equal(I.exposure({exposure_seconds:'0',observed_seconds:'0'}), null);
  assert.equal(I.exposure({exposure_seconds:'0',observed_seconds:'3600'}), 0);
  assert.equal(I.exposure({exposure_seconds:'900',observed_seconds:'3600'}), .25);
  assert.equal(I.exposure({observed_seconds:'3600'}), null);
});

test('ratio chart includes threshold without implying a negative ratio', () => {
  const flat = I.ratioDomain([0, null], 0);
  assert.equal(flat[0], 0); assert.ok(flat[1] > 0);
  const [lo, hi] = I.ratioDomain([.01, .02], .2);
  assert.equal(lo, 0); assert.ok(hi > .2);
  assert.equal(I.ratioDomain([1], null)[1], 1);
});

test('sampling follows latest until the user fixes a timestamp and respects gaps', () => {
  const points = [{ts:10,segment:0,scenarios:{a:{inventory_ratio:0}}},{ts:20,segment:0,scenarios:{}},{ts:40,segment:1,scenarios:{a:{inventory_ratio:.1}}}];
  assert.equal(I.sampleIndex(points, null), 2);
  assert.equal(I.sampleIndex(points, 21), 1);
  assert.equal(I.sampleIndex([], 21), -1);
  const path = M.path(points, p => p.scenarios.a?.inventory_ratio, x => x, y => y);
  assert.equal((path.match(/M/g) || []).length, 2);
  assert.ok(!path.includes('NaN'));
});

test('inventory filters round-trip through the existing URL contract', () => {
  assert.deepEqual(M.state('?strategy=tolerance-5&range=1h&view=parameters', ['tolerance-5']), {strategy:'tolerance-5',range:'1h',view:'parameters'});
  assert.deepEqual(M.state('?strategy=missing&range=forever&view=secret', ['tolerance-5']), {strategy:'all',range:'24h',view:'positions'});
});
