const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function fixture(embedded = true) {
  const events = new Map(), sent = [], changes = [];
  const on = (name, fn) => events.set(name, [...(events.get(name) || []), fn]);
  const document = {hidden:false, addEventListener:on};
  const navigator = {onLine:true};
  const window = {addEventListener:on};
  window.parent = embedded ? {postMessage:(data, origin) => sent.push({data, origin})} : window;
  const context = {window, document, navigator, location:{hostname:embedded ? `p-${'a'.repeat(24)}.hub.localhost` : '127.0.0.1', protocol:'http:', port:'3100'}};
  vm.runInNewContext(fs.readFileSync('variational_grid/web/hub.js', 'utf8'), context);
  window.GridHub.subscribe(active => changes.push(active));
  const fire = (name, value) => {for (const fn of events.get(name) || []) fn(value);};
  const message = (data, extra = {}) => fire('message', {source:window.parent, origin:'http://hub.localhost:3100', data:{channel:'project-hub', version:1, ...data}, ...extra});
  return {...context, sent, changes, fire, message, active:window.GridHub.active};
}

test('embedded page stays idle until exact parent handshakes; forged activity cannot start work', () => {
  const f = fixture();
  assert.equal(f.active(), false);
  f.message({type:'activity', active:true});
  assert.equal(f.active(), false);
  f.message({type:'ready', role:'host'}, {origin:'http://other.localhost:3100'});
  f.message({type:'ready', role:'host'}, {source:{}});
  f.message({type:'activity', active:true});
  assert.equal(f.active(), false);
  f.message({type:'ready', role:'host'});
  assert.deepEqual(Array.from(f.sent.at(-1).data.capabilities), ['activity']);
  assert.equal(f.sent.at(-1).origin, 'http://hub.localhost:3100');
  f.message({type:'activity', active:true});
  f.message({type:'activity', active:true});
  f.message({type:'activity', active:false}, {origin:'http://hub.localhost:9999'});
  assert.deepEqual(f.changes, [true]);
  f.message({type:'activity', active:false});
  assert.deepEqual(f.changes, [true, false]);
});

test('visibility and connectivity pause reads, then resume once; standalone never needs a host', () => {
  for (const embedded of [true, false]) {
    const f = fixture(embedded);
    if (embedded) {f.message({type:'ready', role:'host'}); f.message({type:'activity', active:true});}
    assert.equal(f.active(), true);
    f.document.hidden = true; f.fire('visibilitychange');
    assert.equal(f.active(), false);
    f.navigator.onLine = false; f.fire('offline');
    f.document.hidden = false; f.fire('visibilitychange');
    assert.equal(f.active(), false);
    f.navigator.onLine = true; f.fire('online');
    assert.equal(f.active(), true);
    assert.deepEqual(f.changes.slice(-2), [false, true]);
    if (!embedded) assert.deepEqual(f.sent, []);
  }
});
