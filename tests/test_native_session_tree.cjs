const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');
const {buildSync} = require('esbuild');
const built = buildSync({entryPoints:[path.join(__dirname,'../native/web/session-tree.ts')],bundle:true,write:false,platform:'node',format:'cjs',alias:{'@':path.join(__dirname,'../frontend')}});
const mod={exports:{}};new Function('require','module','exports',built.outputFiles[0].text)(require,mod,mod.exports);
const {sessionTree}=mod.exports;
const row=(id,parent,extra={})=>({id,title:id,owner_id:'me',source:'native',status:'idle',archived:false,parent_session_id:parent,...extra});
const ids=rows=>rows.map(r=>r.id);

test('recent child activity raises its family and each child appears only under its parent',()=>{
  const tree=sessionTree([row('a','parent'),row('other'),row('b','parent'),row('parent'),row('c','parent')]);
  assert.deepEqual(ids(tree.roots),['parent','other']);
  assert.deepEqual(ids(tree.children.get('native:parent')),['a','b','c']);
});
test('nested ancestry stays source-qualified and does not attach to a colliding historical ID',()=>{
  const tree=sessionTree([row('grand','child'),row('child','parent'),row('parent'),row('parent',null,{source:'legacy',read_only:true})]);
  assert.deepEqual(tree.roots.map(r=>r.source+':'+r.id),['native:parent','legacy:parent']);
  assert.deepEqual(ids(tree.children.get('native:child')),['grand']);
  assert.deepEqual(ids(tree.children.get('native:parent')),['child']);
});
test('missing parents and explicit pins or folders preserve independent navigation',()=>{
  const tree=sessionTree([row('orphan','missing'),row('pinned','parent',{pinned:true}),row('filed','parent',{folder:'Today'}),row('parent')]);
  assert.deepEqual(ids(tree.roots),['orphan','pinned','filed','parent']);
  assert.equal(tree.children.size,0);
});
test('malformed ancestry cycles cannot hide rows or recurse forever',()=>{
  const tree=sessionTree([row('a','b'),row('b','a'),row('self','self')]);
  assert.deepEqual(ids(tree.roots),['a','b','self']);
  assert.equal(tree.children.size,0);
});
