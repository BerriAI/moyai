const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const {setImmediate: tick} = require('node:timers/promises');

const source = fs.readFileSync(path.join(__dirname, '../native/web/desktop-input.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const loaded = import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

function gate() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return {promise, release};
}

test('wheel bursts keep one in-flight and one bounded pending batch', async () => {
  const {DesktopInputQueue} = await loaded;
  const first = gate(), second = gate(), calls = [];
  const queue = new DesktopInputQueue(async command => {
    calls.push(command);
    if (calls.length === 1) await first.promise;
    else second.release();
  }, () => true, error => { throw error; });
  queue.wheel(80);
  await tick();
  for (let i = 0; i < 200; i++) queue.wheel(80);
  assert.equal(calls.length, 1);
  first.release();
  await second.promise;
  await queue.drain();
  assert.deepEqual(calls.map(call => call.action), ['scroll', 'scroll']);
  assert.equal(calls[1].steps, 10);
});

test('pointer and key input supersede stale scroll without a wheel backlog', async () => {
  const {DesktopInputQueue} = await loaded;
  const held = gate(), calls = [];
  const queue = new DesktopInputQueue(async command => {
    calls.push(command);
    if (calls.length === 1) await held.promise;
  }, () => true, error => { throw error; });
  queue.wheel(80);
  await tick();
  for (let i = 0; i < 100; i++) queue.wheel(80);
  queue.enqueue('click', {x: 10, y: 20});
  queue.enqueue('key', {key: 'Enter'});
  held.release();
  await queue.drain();
  assert.deepEqual(calls.map(call => call.action), ['scroll', 'click', 'key']);
});

test('release cancels pending movement after the current dispatch', async () => {
  const {DesktopInputQueue} = await loaded;
  const held = gate(), calls = [];
  let controlled = true;
  const queue = new DesktopInputQueue(async command => {
    calls.push(command);
    await held.promise;
  }, () => controlled, error => { throw error; });
  queue.wheel(80);
  await tick();
  for (let i = 0; i < 100; i++) queue.wheel(80);
  controlled = false;
  held.release();
  await queue.drain();
  assert.equal(calls.length, 1);
});
