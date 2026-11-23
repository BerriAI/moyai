const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

function moduleUrl(file, dependencies = {}) {
  let code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../../native/web/conversations', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  for (const [specifier, url] of Object.entries(dependencies)) code = code.replace(JSON.stringify(specifier), JSON.stringify(url));
  return 'data:text/javascript;base64,' + Buffer.from(code).toString('base64');
}
const conversationModule = import(moduleUrl("conversation-state.ts"));
const file = (name, type = 'text/plain') => ({ name, type, size: 24 });
const result = file => ({ id: 'saved-' + file.name, name: file.name, size: file.size, media_type: file.type });

test('ASAP messages waiting behind a question remain visibly queued', async () => {
  const { waitsBehindActive } = await conversationModule;
  const turn = { id: 'next', status: 'queued', mode: 'when_idle' };
  assert.equal(waitsBehindActive(turn), false);
  assert.equal(waitsBehindActive(turn, { id: 'next', status: 'starting' }), false);
  assert.equal(waitsBehindActive(turn, { id: 'previous', status: 'completed' }), false);
  for (const status of ['starting', 'running', 'saving', 'waiting', 'stopping']) {
    const active = { id: 'previous', status };
    assert.equal(waitsBehindActive(turn, active), true);
    assert.equal(waitsBehindActive({ ...turn, mode: 'asap' }, active), status === 'waiting');
    assert.equal(waitsBehindActive(turn, active, true), status === 'waiting');
  }
  assert.equal(waitsBehindActive({ ...turn, status: 'steering' }, { id: 'previous', status: 'running' }), false);
});

test('home-to-chat seed uses the actual accepted turn and preserves its files', async () => {
  const { firstMessage } = await conversationModule;
  const attachments = [result(file('note.md'))];
  const message = firstMessage({ turn: { id: 'server-turn', actor_id: 'server-user' }, prompt: 'Hello', attachments });
  assert.deepEqual(message, {
    id: -1, turn_id: 'server-turn', role: 'user', author_id: 'server-user', content: 'Hello', attachments,
  });
});

test('partial history keeps known pages and receipts until a complete snapshot replaces saved rows', async () => {
  const { reconcileHistory } = await conversationModule;
  const first = { id: 1, turn_id: 'old', role: 'user', content: 'First page' };
  const later = { id: 201, turn_id: 'later', role: 'user', content: 'Known later page' };
  const receipt = { id: -3, turn_id: 'accepted', role: 'user', content: 'Accepted input' };
  const edited = { ...first, content: 'Edited first page' };
  assert.deepEqual(reconcileHistory([first, later, receipt], [edited]), [edited, later, receipt]);
  assert.deepEqual(reconcileHistory([first, later, receipt], [edited], true), [edited, receipt]);
});

test('a saved input reconciles only its own receipt, preserving other concurrent sends', async () => {
  const { reconcileHistory } = await conversationModule;
  const a = { id: -3, turn_id: 'a', role: 'user', content: 'A' };
  const b = { id: -4, turn_id: 'b', role: 'user', content: 'B' };
  const saved = { ...a, id: 221 };
  assert.deepEqual(reconcileHistory([a, b], [saved], true), [saved, b]);
  assert.deepEqual(reconcileHistory([saved, b], [saved]), [saved, b]);
});
