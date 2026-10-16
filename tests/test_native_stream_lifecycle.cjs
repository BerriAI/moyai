const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../native/web/desktop-session.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const loaded = import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('hiding invalidates responses; showing cannot accept responses from the old connection', async () => {
  const {DesktopLifetime} = await loaded;
  const life = new DesktopLifetime(true), old = life.invalidate();
  assert.equal(life.accepts(old), true);
  life.setVisible(false);
  assert.equal(life.accepts(old), false);
  life.setVisible(true);
  assert.equal(life.accepts(old), false);
  const resumed = life.invalidate();
  assert.equal(life.accepts(resumed), true);
  life.dispose();
  assert.equal(life.accepts(resumed), false);
});

test('a newer control request fences an older metadata response', async () => {
  const {DesktopLifetime} = await loaded;
  const life = new DesktopLifetime(true);
  const poll = life.invalidate(), claim = life.invalidate();
  assert.equal(life.accepts(poll), false);
  assert.equal(life.accepts(claim), true);
});

function remote() {
  const sent = [];
  return {
    sent, viewOnly: false,
    clipboardPasteFrom: text => sent.push(['clipboard', text]),
    sendKey: (...args) => sent.push(['key', ...args]),
    focus: () => sent.push(['focus']),
  };
}

test('clipboard text precedes paste keys and injected Control is always released', async () => {
  const {pasteRemote} = await loaded;
  const rfb = remote();
  assert.equal(pasteRemote(rfb, 'Unicode text: café 日本語', true), true);
  assert.deepEqual(rfb.sent, [
    ['clipboard', 'Unicode text: café 日本語'], ['key', 0xffe3, 'ControlLeft', true],
    ['key', 0x76, 'KeyV'], ['key', 0xffe3, 'ControlLeft', false], ['focus'],
  ]);
});

test('viewers, hidden panels and oversized paste never inject remote input', async () => {
  const {pasteRemote} = await loaded;
  const rfb = remote();
  assert.equal(pasteRemote(rfb, 'hidden', false), false);
  rfb.viewOnly = true;
  assert.equal(pasteRemote(rfb, 'viewer', true), false);
  rfb.viewOnly = false;
  assert.throws(() => pasteRemote(rfb, 'x'.repeat(20001), true), /20,000/);
  assert.deepEqual(rfb.sent, []);
});

test('suspension sends held key releases before setting viewOnly and discards local key state', async () => {
  const {pauseRemote} = await loaded;
  const shift = {key: 'Shift', code: 'ShiftLeft', location: 1}, a = {key: 'A', code: 'KeyA', location: 0};
  const rfb = remote(), held = new Map([['ShiftLeft', shift], ['KeyA', a]]);
  const original = rfb.sendKey;
  rfb.sendKey = (...args) => { assert.equal(rfb.viewOnly, false); original(...args); };
  pauseRemote(rfb, held, key => { assert.equal(rfb.viewOnly, false); rfb.sent.push(['keyup', key]); });
  assert.equal(rfb.viewOnly, true);
  assert.equal(held.size, 0);
  assert.deepEqual(rfb.sent, [
    ['keyup', shift], ['keyup', a], ['key', 0xffe3, 'ControlLeft', false],
  ]);
});
