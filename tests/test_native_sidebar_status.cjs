const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');
const {buildSync} = require('esbuild');
const built = buildSync({entryPoints:[path.join(__dirname,'../native/web/sidebar-status.ts')],bundle:true,write:false,platform:'node',format:'cjs'});
const mod={exports:{}};new Function('require','module','exports',built.outputFiles[0].text)(require,mod,mod.exports);
const {sessionStatusGroup,groupSessionFamilies,compactSessionAge}=mod.exports;
const row=(id,status='idle',extra={})=>({id,title:id,owner_id:'me',source:'native',status,archived:false,...extra});
const ready={url:'https://github.com/example/project/pull/1',state:'open',draft:false,accessible:true};

test('current attention and execution take priority over review-ready pull requests',()=>{
  for(const status of ['waiting','failed'])assert.equal(sessionStatusGroup(row(status,status,{pull_requests:[ready]})),'needs-you');
  for(const status of ['queued','running','saving'])assert.equal(sessionStatusGroup(row(status,status,{pull_requests:[ready]})),'in-progress');
  assert.equal(sessionStatusGroup(row('review','idle',{pull_requests:[ready]})),'in-review');
});
test('inaccessible, draft, closed and merged pull requests do not imply review readiness',()=>{
  for(const pr of [{...ready,accessible:false},{...ready,url:''},{...ready,draft:true},{...ready,draft:undefined},
    {...ready,state:'closed'},{...ready,state:'merged'},{...ready,state:'unknown'}])
    assert.equal(sessionStatusGroup(row('other','idle',{pull_requests:[pr]})),'backlog');
  for(const status of ['idle','completed','stopped','unknown'])assert.equal(sessionStatusGroup(row(status,status)),'backlog');
});
test('historical, archived and deleted rows never claim current attention or execution',()=>{
  for(const flags of [{source:'legacy'},{read_only:true},{archived:true},{deleted:true}])
    assert.equal(sessionStatusGroup(row('past','running',{...flags,pull_requests:[ready]})),'backlog');
});
test('families follow the most urgent child without duplicating rows or losing pinned order',()=>{
  const roots=[row('plain'),row('parent'),row('pinned','idle',{pinned:true}),row('review','idle',{pull_requests:[ready]})];
  const children=new Map([['native:parent',[row('child','running')]],['native:child',[row('grandchild','waiting')]]]);
  const groups=groupSessionFamilies(roots,children);
  assert.deepEqual(groups.get('needs-you').map(item=>item.id),['parent']);
  assert.deepEqual(groups.get('in-progress'),[]);
  assert.deepEqual(groups.get('in-review').map(item=>item.id),['review']);
  assert.deepEqual(groups.get('backlog').map(item=>item.id),['pinned','plain']);
  assert.equal([...groups.values()].flat().length,roots.length);
});
test('compact update times handle invalid and future timestamps without misleading NaN labels',()=>{
  const now=Date.parse('2026-10-17T12:00:00Z');
  assert.equal(compactSessionAge(undefined,now),'');assert.equal(compactSessionAge('invalid',now),'');
  assert.equal(compactSessionAge('2026-10-17T12:01:00Z',now),'now');
  assert.equal(compactSessionAge('2026-10-17T11:57:00Z',now),'3m');
  assert.equal(compactSessionAge('2026-10-17T09:00:00Z',now),'3h');
  assert.equal(compactSessionAge('2026-10-15T12:00:00Z',now),'2d');
});
