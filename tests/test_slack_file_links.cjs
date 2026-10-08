const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('./helpers/ui-vm.cjs');
const {readFileSync}=require('node:fs');
const MoyaiFiles=require('../app/static/saved-files.js');
const runId='a'.repeat(32),fileRef='/workspace/nightly-preflight/cutover-status.md';
const link=ref=>`#run=${runId}&file=${encodeURIComponent(ref)}`;

test('file links round-trip encoded paths without accepting other destinations',()=>{
  for(const ref of [fileRef,'./report.markdown','notes%20%26%20decisions.md','café.md','report.md#L2-L4']){
    const parsed=MoyaiFiles.sessionLink(link(ref));
    assert.equal(parsed.fileRef,ref);assert.equal(parsed.runId,runId);
    assert.deepEqual(MoyaiFiles.reference(parsed.fileRef),MoyaiFiles.reference(ref));
  }
  for(const ref of ['/etc/report.md','../report.md','repo//report.md','https://evil.test/file.md','report.md?token=x','report.md#L3-L2','report%ff.md','report%00.md','a\\b.md'])assert.equal(MoyaiFiles.sessionLink(link(ref)),null,ref);
  for(const hash of [link(fileRef)+'&file=other.md',link(fileRef)+'&credential='+'b'.repeat(32),link(fileRef)+'\n','#run=other&file=report.md',`#run=${runId}&file=%zz`])assert.equal(MoyaiFiles.sessionLink(hash),null,hash);
});

function fixture(){
  const calls=[],paths=[],state={pageVersion:0,runs:[{id:runId}],expandedParents:new Set()},file={archive_path:'new-files/nightly-preflight/cutover-status.md',workspace_path:'nightly-preflight/cutover-status.md',name:'cutover-status.md'};
  const context={state,MoyaiFiles,stopStream(){},document:{hidden:true},history:{replaceState:(_,__,path)=>paths.push(path)},setView(){},sessionTitle:()=>'',renderChat(){calls.push('render');},
    api:async url=>url.endsWith('/files')?{files:[file]}:{id:runId,chat_enabled:true},
    savedFiles:{open:async(f,ref)=>calls.push({file:f,ref})},workspacePanel:{open:kind=>calls.push(kind)},toast:message=>calls.push(message)};
  vm.createContext(context);vm.runInContext(readFileSync('app/static/credentials.js','utf8'),context);
  const app=readFileSync('app/static/app.js','utf8');vm.runInContext(app.slice(app.indexOf('async function openRun('),app.indexOf('function renderChat(')),context);
  return {context,state,calls,paths,file};
}

test('opening a session link preserves its target and opens the exact saved file',async()=>{
  const f=fixture();await f.context.openRun(runId,link(fileRef));
  assert.deepEqual(f.paths,[link(fileRef),link(fileRef)]);
  assert.equal(f.calls[0],'render');assert.equal(f.calls[1].file,f.file);assert.equal(f.calls[1].ref,fileRef);
});

test('missing and ambiguous references show available files, never an arbitrary document',async()=>{
  for(const files of [[],[{workspace_path:'a/report.md',name:'report.md'},{workspace_path:'b/report.md',name:'report.md'}]]){
    const f=fixture();f.state.pageVersion=1;f.state.selected=runId;f.context.api=async()=>({files});
    await f.context.openLinkedFile(MoyaiFiles.sessionLink(link('report.md')),1);
    assert.equal(f.calls[0],'files');assert.match(f.calls[1],/not available/);
  }
});

test('a file lookup finishing after navigation cannot open a file in another session',async()=>{
  const f=fixture();let resolve;f.context.api=()=>new Promise(done=>resolve=done);f.state.pageVersion=1;f.state.selected=runId;
  const pending=f.context.openLinkedFile(MoyaiFiles.sessionLink(link(fileRef)),1);
  f.state.pageVersion++;f.state.selected='b'.repeat(32);resolve({files:[f.file]});await pending;
  assert.deepEqual(f.calls,[]);
});
