const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../native/web/file-links.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const loaded = import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const origin = 'http://127.0.0.1:8807';
const file = 'efbc22e2-15c7-49df-b562-999440ef1f14';
const session = '4812cd2e-8d3c-4cd7-b0ea-1dc3759a6ce3';

test('relative and absolute saved-file URLs route to the current authorized session', async () => {
  const { fileReference } = await loaded;
  for (const href of [`/api/files/${file}`, `api/files/${file}`, `${origin}/api/files/${file}`]) {
    assert.deepEqual(fileReference(href, session, origin), {
      kind: 'artifact', id: file, sessionId: session, url: `/api/files/${file}`,
    });
  }
});

test('uploaded screenshots use upload identity instead of the artifact route', async () => {
  const { fileReference } = await loaded;
  assert.deepEqual(fileReference(`/api/uploads/${file}`, session, origin), {
    kind: 'upload', id: file, sessionId: session, url: `/api/uploads/${file}`,
  });
});

test('a scoped file link retains its source session in a different host chat', async () => {
  const { fileReference } = await loaded;
  for (const kind of ['artifact', 'upload']) {
    for (const suffix of ['', '/content', '/download', '/download/']) {
      const href = `/api/sessions/child-session/file-preview/${kind}/${file}${suffix}`;
      const target = fileReference(href, session, origin);
      assert.equal(target.sessionId, 'child-session');
      assert.equal(target.kind, kind);
      assert.equal(target.id, file);
    }
  }
});

test('query strings never become preview destinations or media URLs', async () => {
  const { fileReference } = await loaded;
  assert.equal(fileReference(`/api/files/${file}?next=https://elsewhere.invalid#section`, session, origin).url, `/api/files/${file}`);
});

test('file extensions on external or arbitrary local links do not trigger preview', async () => {
  const { fileReference } = await loaded;
  for (const href of [
    'https://elsewhere.invalid/demo.mp4', 'https://elsewhere.invalid/screenshot.png',
    '/workspace/report.md', '/report.txt', '/api/files/file.txt',
    `https://elsewhere.invalid/api/files/${file}`, `http://127.0.0.1:8796/api/files/${file}`,
    '/#settings', '#section',
  ]) assert.equal(fileReference(href, session, origin), null, href);
});

test('credential-bearing, unsafe and ambiguous paths cannot become a file preview', async () => {
  const { fileReference } = await loaded;
  for (const href of [
    `http://user:password@127.0.0.1:8807/api/files/${file}`,
    'javascript:alert(1)', 'data:text/html,file', 'blob:http://127.0.0.1:8807/id',
    `/api/files/${file}/extra`, '/api/files/%2e%2e', '/api/files/a%2fb',
    '/api/files/../../settings', '/api/files/', `/api/files/${file}\n`,
    `/api\\files\\${file}`, '//elsewhere.invalid/api/files/id',
  ]) assert.equal(fileReference(href, session, origin), null, href);
  assert.equal(fileReference(`/api/files/${file}`, '', origin), null);
});
