// Supplementary pure-route/component checks. Real application evidence is separate.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const root = path.resolve(__dirname, '..');
const original = 'a'.repeat(32), native = '12345678-1234-1234-1234-123456789abc';
let helpers, server, history;
test.before(async () => {
  const file = ts.createSourceFile('main.tsx', fs.readFileSync(path.join(root, 'native/web/main.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const wanted = new Set(['sessionReferenceHash', 'continuationReference']);
  const source = file.statements.filter(node => ts.isFunctionDeclaration(node) && wanted.has(node.name?.text)).map(node => node.getText(file)).join('\n');
  const code = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText;
  helpers = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
  const {createServer} = await import('vite');
  server = await createServer({configFile: path.join(root, 'native/web/vite.config.ts'), optimizeDeps: {noDiscovery: true, include: []}, server: {middlewareMode: true}, appType: 'custom'});
  history = await server.ssrLoadModule('/product-history.tsx');
});
test.after(async () => { await server?.close(); });

test('server-issued original URL is retained for native search and copy navigation', () => {
  assert.equal(helpers.sessionReferenceHash({source: 'native', id: native, url: '/#run=' + original}), 'run=' + original);
  assert.equal(helpers.sessionReferenceHash({source: 'legacy', id: original}), 'run=' + original);
  assert.equal(helpers.sessionReferenceHash({source: 'native', id: native}), native);
  for (const url of ['https://evil.test/#run=' + original, '//#run=' + original, '/#run=../other', '/#run=' + original + '?key=x']) {
    assert.equal(helpers.sessionReferenceHash({source: 'native', id: native, url}), native);
  }
});

test('a continuation reference must agree on source, native UUID, original identity, and canonical local URL', () => {
  const reference = {source: 'native', id: native, source_session_id: original, url: '/#run=' + original};
  assert.deepEqual(helpers.continuationReference(reference, original), reference);
  for (const patch of [
    {source: 'legacy'}, {id: original}, {source_session_id: 'b'.repeat(32)},
    {url: 'https://evil.test/#run=' + original}, {url: '/#' + native}, {source_session_id: undefined},
  ]) assert.equal(helpers.continuationReference({...reference, ...patch}, original), null);
  assert.equal(helpers.continuationReference(reference, '../other'), null);
});

test('collapsed earlier-history disclosure does not mount or fetch private content', () => {
  const html = renderToStaticMarkup(React.createElement(history.ProductHistoryPrefix, {sessionId: original, scopeKey: 'current-authority'}));
  assert.match(html, /Earlier conversation and files/);
  assert.match(html, /<details/);
  assert.doesNotMatch(html, /Read-only history|Loading history|Existing session history|<h1/);
});

test('retained workspace downloads remain same-origin and scoped to the displayed original session', () => {
  global.window = {location: {origin: 'http://localhost:8837'}};
  try {
    const recovery = `/api/product/sessions/${original}/continuation/archive/${'b'.repeat(32)}`;
    assert.equal(history.productFileUrl(recovery, original), recovery);
    for (const value of [
      recovery.replace(original, 'c'.repeat(32)), recovery + '/extra',
      'https://evil.test' + recovery, '//evil.test' + recovery,
      `/api/product/sessions/${original}/continuation/archive/../../other`,
    ]) assert.equal(history.productFileUrl(value, original), undefined);
  } finally { delete global.window; }
});
