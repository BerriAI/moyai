const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync('native/web/polling.ts', 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const loaded = import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const settle = async () => { await Promise.resolve(); await Promise.resolve(); };

test('hidden reads abort, resume immediately, and late completion cannot create a second poll', async t => {
  const {pollVisible} = await loaded;
  const document = new EventTarget();
  document.hidden = false;
  global.document = document;
  t.mock.timers.enable({apis:['setTimeout']});
  const requests = [], errors = [];
  const stop = pollVisible(signal => new Promise(resolve => requests.push({signal, resolve})), () => 100, e => errors.push(e));
  t.after(() => { stop(); delete global.document; });
  assert.equal(requests.length, 1);
  document.hidden = true; document.dispatchEvent(new Event('visibilitychange'));
  assert.equal(requests[0].signal.aborted, true);
  t.mock.timers.tick(1000); assert.equal(requests.length, 1);
  document.hidden = false; document.dispatchEvent(new Event('visibilitychange'));
  assert.equal(requests.length, 2);
  requests[0].resolve(); requests[1].resolve(); await settle();
  t.mock.timers.tick(100); assert.equal(requests.length, 3);
  stop(); requests[2].resolve(); await settle();
  t.mock.timers.tick(1000); assert.equal(requests.length, 3);
  assert.deepEqual(errors, []);
});
