const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

function moduleUrl(file, dependencies = {}) {
  let code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../native/web', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  for (const [specifier, url] of Object.entries(dependencies)) code = code.replace(JSON.stringify(specifier), JSON.stringify(url));
  return 'data:text/javascript;base64,' + Buffer.from(code).toString('base64');
}
const queueModule = import(moduleUrl('attachment-uploads.ts', { './attachment-files': moduleUrl('attachment-files.ts') }));
const conversationModule = import(moduleUrl('conversation-state.ts'));
const tick = () => new Promise(resolve => setImmediate(resolve));
const file = (name, type = 'text/plain') => ({ name, type, size: 24 });
const result = file => ({ id: 'saved-' + file.name, name: file.name, size: file.size, media_type: file.type });

async function uploads() {
  const { AttachmentUploads } = await queueModule;
  const calls = [], changes = [], revoked = [];
  const queue = new AttachmentUploads(
    files => changes.push(files),
    (file, signal) => new Promise((resolve, reject) => calls.push({ file, signal, resolve, reject })),
    { create: file => 'blob:test/' + file.name, revoke: url => revoked.push(url) },
  );
  return { queue, calls, changes, revoked };
}

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

test('all upload cards appear immediately, with bounded parallel requests and stable order', async () => {
  const { queue, calls, changes } = await uploads();
  const files = ['a.txt', 'b.txt', 'c.txt', 'd.txt'].map(name => file(name));
  const finished = queue.add(files);
  assert.equal(changes.length, 1);
  assert.equal(changes[0].length, 4);
  assert.ok(queue.files.every(item => item.uploadState === 'uploading'));
  assert.equal(calls.length, 3);
  calls[1].resolve(result(files[1]));
  await tick();
  assert.equal(calls.length, 4);
  assert.equal(queue.files[1].uploadState, 'ready');
  for (const index of [3, 0, 2]) calls[index].resolve(result(files[index]));
  await finished;
  assert.deepEqual(queue.files.map(item => item.name), files.map(item => item.name));
  assert.ok(queue.files.every(item => item.uploadState === 'ready' && item.id.startsWith('saved-')));
});

test('removing an uploading card aborts it and a late success cannot restore it', async () => {
  const { queue, calls } = await uploads();
  const finished = queue.add([file('cancel.txt')]);
  queue.remove(queue.files[0].id);
  assert.ok(calls[0].signal.aborted);
  assert.equal(queue.files.length, 0);
  calls[0].resolve(result(file('cancel.txt')));
  await finished; await tick();
  assert.equal(queue.files.length, 0);
});

test('removing a waiting upload never starts its network request', async () => {
  const { queue, calls } = await uploads();
  const finished = queue.add(['a', 'b', 'c', 'cancelled'].map(name => file(name)));
  queue.remove(queue.files[3].id);
  for (const call of calls) call.resolve(result(call.file));
  await finished; await tick();
  assert.equal(calls.length, 3);
  assert.equal(queue.files.length, 3);
});

test('a failed file retains its card and retry replaces that card without duplicating it', async () => {
  const { queue, calls } = await uploads();
  const finished = queue.add([file('retry.txt')]);
  const localId = queue.files[0].localId;
  calls[0].reject(new Error('Temporary upload failure'));
  await finished;
  assert.equal(queue.files[0].uploadState, 'failed');
  assert.equal(queue.files[0].error, 'Temporary upload failure');
  queue.retry(localId);
  assert.equal(queue.files[0].uploadState, 'uploading');
  calls[1].resolve(result(file('retry.txt')));
  await tick();
  assert.equal(queue.files.length, 1);
  assert.equal(queue.files[0].localId, localId);
  assert.equal(queue.files[0].id, 'saved-retry.txt');
});

test('local thumbnails survive upload completion and are released on clear or unmount', async () => {
  const { queue, calls, revoked } = await uploads();
  const finished = queue.add([file('image.png', 'image/png')]);
  assert.equal(queue.files[0].previewUrl, 'blob:test/image.png');
  calls[0].resolve(result(file('image.png', 'image/png')));
  await finished;
  assert.equal(queue.files[0].previewUrl, 'blob:test/image.png');
  assert.deepEqual(revoked, []);
  queue.clear();
  queue.dispose();
  assert.deepEqual(revoked, ['blob:test/image.png']);
});

test('unmount aborts pending requests and suppresses late UI updates', async () => {
  const { queue, calls, changes } = await uploads();
  const finished = queue.add([file('private.txt')]);
  const count = changes.length;
  queue.dispose();
  calls[0].resolve(result(file('private.txt')));
  await finished; await tick();
  assert.ok(calls[0].signal.aborted);
  assert.equal(queue.files.length, 0);
  assert.equal(changes.length, count);
});

test('pending and failed cards count toward the same per-message limit', async () => {
  const { queue, calls } = await uploads();
  const finished = queue.add(Array.from({ length: 8 }, (_, index) => file(String(index))));
  await assert.rejects(queue.add([file('extra')]), /up to 8 files/);
  queue.dispose();
  for (const call of calls) call.reject(new Error('Cancelled'));
  await finished;
});
