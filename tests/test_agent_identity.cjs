const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
function load(){
  const context={};vm.createContext(context);
  vm.runInContext(readFileSync('app/static/icons.js','utf8'),context);
  return context.MoyaiAgentIcon;
}
test('agent identity survives fresh loads and unrelated rendering',()=>{
  const icon=load(),id='b'.repeat(32),expected=icon(id);
  for(const other of ['c','d','e'])icon(other.repeat(32));
  assert.equal(icon(id),expected);
  assert.equal(load()(id),expected);
  const identities=['b','c','d','e'].map(x=>icon(x.repeat(32)));
  assert.equal(new Set(identities).size,4);
});
test('agent IDs cannot introduce markup and icons remain decorative',()=>{
  const html=load()('<img src=x onerror=alert(1)>');
  assert.doesNotMatch(html,/<img|onerror|alert/);
  assert.match(html,/aria-hidden="true"/);
  assert.match(html,/focusable="false"/);
});
