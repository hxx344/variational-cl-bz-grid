/* Execute the browser entry point, including fetch, timers and rendered states. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const M = require('../variational_grid/web/model.js');
const flush = () => new Promise(resolve => setImmediate(resolve));

function page() {
  const nodes = new Map(), requests = [], timers = new Map(), events = {};
  let serial = 0, active = true, activity;
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {textContent:'',innerHTML:'',children:[],dataset:{},clientWidth:500,
      setAttribute() {}, getAttribute() {return id;}, addEventListener() {}, querySelectorAll() {return [];}});
    return nodes.get(id);
  }
  const location = {search:''};
  const document = {getElementById:node, querySelector:node, querySelectorAll:() => [],
    addEventListener:(name, fn) => events[name] = fn, activeElement:null};
  const window = {GridModel:M,GridHub:{active:() => active,subscribe:fn => activity = fn},addEventListener() {}};
  vm.runInNewContext(fs.readFileSync('variational_grid/web/qqq.js', 'utf8'), {
    document,window,location,URLSearchParams,AbortController,Date,performance,
    history:{pushState:(_a,_b,url) => location.search = url},ResizeObserver:class {observe() {}},
    setTimeout:(fn,ms) => {timers.set(++serial,{fn,ms}); return serial;},
    clearTimeout:id => timers.delete(id),setInterval() {},
    fetch:(url,{signal}) => new Promise((resolve,reject) => {
      const request = {url,signal,resolve,reject}; requests.push(request);
      signal.addEventListener('abort',() => reject(Object.assign(new Error('aborted'),{name:'AbortError'})));
    }),
  });
  return {node,requests,respond(request,payload,status=200) {request.resolve({ok:status===200,status,json:async() => payload});},
    timer(ms,name) {const item = [...timers].find(([,t]) => t.ms===ms && (!name || t.fn.name===name)); assert.ok(item,`missing timer ${ms} ${name}`); timers.delete(item[0]); item[1].fn();},
    range(range) {events.click({target:{closest:selector => selector==='[data-range]' ? {dataset:{range}} : null}});},
    active(value) {active=value;activity(value);}};
}
function snapshot(mark=743,generation='g1') {
  const ts = Math.floor(Date.now()/1000);
  return {server_ts:ts,reset:{generation,status:'idle'},runtime:{status:'running'},details_available:true,positions:[],trades:[],
    history:{range:'24h',names:[],source_count:0,points:[]},summary:{kind:'qqq_hedge',ts,poll_seconds:2,parameters:{},
      market:{qqq_mark:String(mark),qqq_source_ts:ts,source_status:'ready'},
      scenarios:[{name:'a',grid_step_percent:'.05',hedge_threshold_usdc:'3000',total_pnl_usdc:'1',qqq:{},us100:{}}]}};
}
function history(request,generation='g1') {
  const query = new URL(request.url,'http://localhost').searchParams;
  const ts = Number(query.get('through'));
  return {reset:{generation,status:'idle'},summary_ts:ts,history:{range:query.get('range'),names:['a'],source_count:1,
    points:[{ts,segment:0,pnl:[1],exposure:[2],net_exposure:[3]}]}};
}

test('slow history never delays first render or restarts during snapshot refresh; failures recover separately', async() => {
  const p=page(); assert.equal(p.requests[0].url,'/api/qqq-snapshot');
  p.respond(p.requests[0],snapshot()); await flush();
  assert.equal(p.node('qqq-mark').textContent,'743.00');
  assert.match(p.node('pnl-chart').innerHTML,/历史曲线正在加载/);
  const h=p.requests[1]; assert.match(h.url,/api\/qqq-history/);
  p.node('refresh').onclick(); p.respond(p.requests[2],snapshot(744)); await flush();
  assert.equal(p.requests.length,3); assert.equal(h.signal.aborted,false);
  assert.equal(p.node('qqq-mark').textContent,'744.00');
  p.respond(h,{error:'history_unavailable'},503); await flush();
  assert.match(p.node('history-note').textContent,/HTTP 503/);
  assert.doesNotMatch(p.node('status').textContent,/连接中断/);
  assert.equal(p.node('qqq-mark').textContent,'744.00');
  // Force the due time forward without a wall-clock wait.
  p.active(false); p.active(true); p.respond(p.requests.at(-1),snapshot(745)); await flush();
  const recovered=p.requests.at(-1); p.respond(recovered,history(recovered)); await flush();
  assert.match(p.node('pnl-chart').innerHTML,/<svg/);
  assert.doesNotMatch(p.node('history-note').textContent,/HTTP 503/);
});

test('history timeout leaves current accounts visible and reports it only in the chart', async() => {
  const p=page(); p.respond(p.requests[0],snapshot()); await flush();
  p.timer(30000); await flush();
  assert.match(p.node('history-note').textContent,/历史曲线读取超时/);
  assert.equal(p.node('qqq-mark').textContent,'743.00');
  assert.doesNotMatch(p.node('status').textContent,/连接中断/);
});

test('late old window and reset-generation histories cannot overwrite current charts', async() => {
  const p=page(); p.respond(p.requests[0],snapshot()); await flush(); const old=p.requests[1];
  p.range('1h'); p.respond(p.requests[2],snapshot(744)); await flush();
  p.respond(old,history(old)); await flush();
  assert.doesNotMatch(p.node('pnl-chart').innerHTML,/<svg/);
  p.timer(0,'refreshHistory'); const pending=p.requests.at(-1); assert.match(pending.url,/range=1h/);
  p.node('refresh').onclick(); p.respond(p.requests.at(-1),snapshot(745,'g2')); await flush();
  p.respond(pending,history(pending)); await flush();
  assert.doesNotMatch(p.node('pnl-chart').innerHTML,/<svg/);
  p.timer(0,'refreshHistory'); const current=p.requests.at(-1); p.respond(current,history(current,'g2')); await flush();
  assert.match(p.node('pnl-chart').innerHTML,/<svg/);
  assert.match(p.node('history-note').textContent,/窗口 1 小时/);
});

test('a failed new window retains the previous curve with its actual range and timestamp', async() => {
  const p=page(); p.respond(p.requests[0],snapshot()); await flush();
  const good=p.requests[1]; p.respond(good,history(good)); await flush();
  p.range('7d'); p.respond(p.requests.at(-1),snapshot(744)); await flush();
  p.respond(p.requests.at(-1),{error:'history_timeout'},503); await flush();
  assert.match(p.node('pnl-chart').innerHTML,/<svg/);
  assert.match(p.node('history-note').textContent,/窗口 24 小时/);
  assert.match(p.node('history-note').textContent,/保留上次成功曲线，截至/);
  assert.equal(p.node('qqq-mark').textContent,'744.00');
});

test('expired portal authentication is distinguished from a market closure or network timeout', async() => {
  const p=page(); p.respond(p.requests[0],{},401); await flush();
  assert.match(p.node('notice').textContent,/页面访问授权失效，请从工作台重新打开此项目/);
});
