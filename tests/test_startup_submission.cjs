const assert=require('node:assert/strict');
const {test}=require('node:test');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('app/static/app.js','utf8');
const submit=source.slice(source.indexOf('async function submitTask('),source.indexOf('\nasync function openRun('));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function setup(){
  const fields=Object.fromEntries(Object.entries({'#prompt':'Start now','#repo':'','#project-environment':'auto','#mode':'modal','#new-model':'test-model','#new-harness':''}).map(([k,value])=>[k,{value}]));
  fields['#retry-create']={};fields['#edit-create']={};
  const requests=[],paint=[],opened=[],cleared=[];
  const context={state:{pageVersion:0,sending:new Set(),attachments:{ids:()=>['file-1'],lock(){},clear(ids){cleared.push(ids);}},newDraft:{}},
    $:id=>fields[id],document:{querySelectorAll:()=>[]},crypto:{randomUUID:()=> 'client-1'},stopStream(){},setView(){},sessionTitle:()=> 'Start now',
    renderSessionSubmission:(...args)=>paint.push(args),refreshRuns:()=>new Promise(()=>{}),openRun:async(...args)=>opened.push(args),
    api:(_path,options)=>{const d=deferred();requests.push({body:JSON.parse(options.body),...d});return d.promise;},showError(){},toast(){},navigate:async()=>{}};
  vm.createContext(context);vm.runInContext(submit,context);
  return {context,fields,requests,paint,opened,cleared};
}
test('submitted text paints before acknowledgement and a held sidebar never delays opening',async()=>{
  const s=setup();const task=s.context.submitTask({preventDefault(){}});
  assert.equal(s.paint[0][0].prompt,'Start now');
  assert.equal(s.context.state.newDraft.prompt,'Start now');
  assert.equal(s.cleared.length,0);assert.equal(s.opened.length,0);
  s.requests[0].resolve({id:'saved'});await task;
  assert.equal(s.opened[0][0],'saved');assert.equal(s.cleared.length,1);
});
test('lost acknowledgement retains attachment IDs and retries the same idempotent input',async()=>{
  const s=setup();const task=s.context.submitTask({preventDefault(){}});
  s.requests[0].reject(new Error('Connection lost'));await task;
  assert.equal(s.cleared.length,0);assert.equal(s.paint.at(-1)[2],true);
  const retry=s.fields['#retry-create'].onclick();
  assert.deepEqual(s.requests[1].body,s.requests[0].body);
  s.requests[1].resolve({id:'saved'});await retry;
  assert.equal(s.opened.length,1);assert.equal(s.cleared.length,1);
});
test('leaving during creation preserves the next draft and never pulls navigation back',async()=>{
  const s=setup();const task=s.context.submitTask({preventDefault(){}});
  s.context.state.pageVersion++;
  s.context.state.newDraft={prompt:'Next question'};
  s.requests[0].resolve({id:'saved'});await task;
  assert.equal(s.context.state.newDraft.prompt,'Next question');assert.equal(s.opened.length,0);
});
