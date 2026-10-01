const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function setup() {
  const elements = new Map();
  const context = {state:{pageVersion:1,role:'admin',identity:{email:'alex@example.test'}},
    $:key => {if(!elements.has(key))elements.set(key,{});return elements.get(key);},
    esc: value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};
  vm.createContext(context);
  vm.runInContext(readFileSync('app/static/users.js','utf8'),context);
  return {context,elements};
}

test('directory search and role filters combine without exposing mismatched rows', () => {
  const {context} = setup();
  const users = [{email:'alex@example.test',name:'Alex',role:'admin'}, {email:'sam@example.test',name:'Sam',role:'member'}];
  const html = context.userTable(users,'  EXAMPLE.TEST ','member');
  assert.match(html,/sam@example.test/);
  assert.doesNotMatch(html,/alex@example.test/);
  assert.match(context.userTable(users,'unknown','all'),/No users match/);
});

test('untrusted profile names and addresses cannot inject HTML into the directory', () => {
  const {context} = setup();
  const html = context.userTable([{email:'person"@example.test',name:'<img src=x onerror=alert(1)>',role:'member'}],'','all');
  assert.doesNotMatch(html,/<img/);
  assert.match(html,/&lt;img/);
  assert.match(html,/person&quot;@example.test/);
});

test('an already-open users page loses admin controls after a demotion', async () => {
  const {context,elements} = setup();
  const paths=[];
  context.api=async path=>{paths.push(path);return {authenticated:true,role:'member',identity:{email:'alex@example.test'},csrf:'new'};};
  await context.renderUsers();
  assert.deepEqual(paths,['/api/session']);
  assert.equal(context.state.role,'member');
  assert.equal(elements.get('#users-nav').hidden,true);
  assert.equal(elements.get('#spend-nav').hidden,true);
  assert.match(elements.get('#content').innerHTML,/Only administrators/);
});

test('navigation away during access check cannot replace the next page', async () => {
  const {context,elements} = setup();
  context.api=async ()=>{context.state.pageVersion++;return {authenticated:true,role:'admin'};};
  await context.renderUsers();
  assert.equal(elements.has('#content'),false);
});
