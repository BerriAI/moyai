const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

function load(relative) {
  const filename = path.resolve(__dirname, '..', relative);
  const module = {exports: {}};
  const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
  }).outputText;
  new Function('require', 'module', 'exports', source)(name => {
    if (name === './analytics-format') return load('native/web/analytics-format.ts');
    // These tests exercise the production segment arithmetic, not rendered UI.
    if (name.startsWith('@/') || name === './analytics-shared') return {};
    return require(name);
  }, module, module.exports);
  return module.exports;
}
const {trendSegments} = load('native/web/performance-trend.tsx');

test('native timing trends leave gaps between unobserved intervals', () => {
  assert.deepEqual(trendSegments([null, 15, 20, null, 4, null, 9, 12]), [[1, 2], [4], [6, 7]]);
  assert.deepEqual(trendSegments([null, null]), []);
});
test('a real zero stays measured while invalid values do not draw a line', () => {
  assert.deepEqual(trendSegments([0, 0, NaN, Infinity, -1, 3]), [[0, 1], [5]]);
});
