const assert=require('node:assert/strict');
const {test}=require('node:test');
const {reference,resolve,create}=require('../app/static/saved-files.js');
const file=path=>({path,workspace_path:path,name:path.split('/').at(-1),archive_path:'new-files/'+path,url:'/download/'+encodeURIComponent(path)});

test('local file references resolve only to the exact saved workspace file',()=>{
  const root=file('design.md'),nested=file('repo/design.md'),files=[root,nested,file('repo/other.txt')];
  for(const value of ['design.md','./design.md','/workspace/design.md','sandbox:/workspace/design.md'])assert.equal(resolve(value,files),root);
  assert.equal(resolve('/workspace/repo/design.md',files),nested);
  assert.equal(resolve('other.txt',files),files[2]);
  assert.equal(resolve('missing/design.md',files),null);
  assert.equal(resolve('DESIGN.md',files),null);
  assert.equal(resolve('design.md',[nested,file('second/design.md')]),null);
  assert.equal(resolve('result.md',[{name:'result.md',path:'result.md',workspace_path:null}]),null);
  assert.equal(resolve('space%20here.md',[file('space here.md')]).name,'space here.md');
});

test('references cannot open arbitrary URLs, paths, or archive traversal',()=>{
  for(const value of ['https://evil.test/design.md','javascript:alert(1)','data:text/html,hi','file:///etc/passwd',
    '//evil.test/design.md','/etc/passwd','../design.md','repo/../design.md','./../design.md','a\\b',
    '/api/runs/other/artifact','design.md?key=x','design.md#x','%2e%2e/design.md','%2f%2fevil.test/x',
    'bad%zz','evil\nfile.md','a//b','a/./b'])assert.equal(reference(value),null,value);
});

test('archive requests are deduplicated and stale responses cannot change another session',async()=>{
  const requests=[],pending=[];
  const button={};const area={innerHTML:'',querySelector:()=>null};
  global.document={addEventListener(){},querySelector:selector=>selector==='#files-button'?button:selector==='#artifact-area'?area:null};
  const view=create({api:url=>{requests.push(url);return new Promise(resolve=>pending.push(resolve));},markdown:s=>s,escape:s=>s,size:s=>String(s)});
  const first={id:'first',has_artifact:true,events:[{id:1,kind:'artifact'}]};
  view.sync(first);view.sync(first);assert.equal(requests.length,1);
  view.reset();view.sync({...first,id:'second'});assert.equal(requests.length,2);
  pending[1]({files:[file('second.md')],revision:'two'});await new Promise(setImmediate);
  pending[0]({files:[file('wrong.md'),file('wrong2.md')],revision:'one'});await new Promise(setImmediate);
  assert.equal(button.textContent,'Files · 1');assert.match(area.innerHTML,/runs\/second\/artifact/);
  view.sync({...first,id:'second',events:[{id:2,kind:'artifact'}]});assert.equal(requests.length,3);
  pending[2]({files:[],revision:'three'});await new Promise(setImmediate);
  assert.equal(button.textContent,'Files · 0');
  delete global.document;
});
