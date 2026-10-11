const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
function setup(status='pending'){
 const buttons=[],continuations=[];
 const area={dataset:{},innerHTML:'',querySelectorAll:selector=>selector==='[data-pr-access]'?buttons:continuations};
 const input={value:'',focus(){this.focused=true},dispatchEvent(){}};
 const context={state:{selected:'chat',pageVersion:1},$:selector=>selector==='#pr-write-access'?area:input,
  esc:value=>String(value).replace(/[<>"&]/g,c=>({'<':'&lt;','>':'&gt;','"':'&quot;','&':'&amp;'}[c])),
  api:async()=>{},toast:()=>{},Event:class{}};
 vm.createContext(context);
 const script=readFileSync('app/static/app.js','utf8');
 vm.runInContext(script.slice(script.indexOf('function renderPrWriteAccess('),script.indexOf('async function changeSessionScope()')),context);
 const run={id:'chat',pr_write_access:[{id:'scope',repository:'org/repo',number:7,title:'<img onerror=alert(1)>',head_repository_id:9,branch:'<script>',base_branch:'main',status}]};
 return {context,area,input,run,buttons,continuations};
}
test('only pending consent is shown with two decisions and escaped scope',()=>{
 for(const status of ['pending','approved','denied','revoked']){
  const {context,area,run}=setup(status);context.renderPrWriteAccess(run);
  if(status!=='pending'){assert.equal(area.innerHTML,'');continue;}
  assert.match(area.innerHTML,/org\/repo #7/);assert.match(area.innerHTML,/this session/);
  assert.doesNotMatch(area.innerHTML,/<img|<script>/);
  assert.match(area.innerHTML,/>Approve</);assert.match(area.innerHTML,/>Not Approve</);
  assert.doesNotMatch(area.innerHTML,/Continue in chat|Revoke access/);
 }
});
test('approval responses cannot replace a newly opened chat',async()=>{
 const {context,area,run,buttons}=setup();buttons.push({dataset:{prAccess:'scope',decision:'approve'}});
 let resolve;context.api=async(path,options)=>options?new Promise(r=>resolve=r):run;context.renderPrWriteAccess(run);
 const pending=buttons[0].onclick();context.state.pageVersion++;context.state.selected='other';
 area.innerHTML='other chat';resolve({status:'approved'});await pending;
 assert.equal(area.innerHTML,'other chat');
});
for(const decision of ['approve','deny'])test(`${decision} dismisses the card without touching the draft`,async()=>{
 const {context,area,run,input,buttons}=setup();
 buttons.push({dataset:{prAccess:'scope',decision}}, {dataset:{prAccess:'scope',decision:'deny'}});
 input.value='My unsent draft';const calls=[];
 context.api=async(path,options)=>{calls.push([path,options]);return {...run,pr_write_access:[{...run.pr_write_access[0],status:decision==='approve'?'approved':'denied'}]};};
 context.renderPrWriteAccess(run);const pending=buttons[0].onclick();
 assert.ok(buttons.every(button=>button.disabled));await pending;
 assert.deepEqual(JSON.parse(calls[0][1].body),{decision,resume:decision==='approve'});
 assert.equal(area.innerHTML,'');assert.equal(input.value,'My unsent draft');
});
test('failed decision keeps both actions available for retry',async()=>{
 const {context,area,run,buttons}=setup();let message;
 buttons.push({dataset:{prAccess:'scope',decision:'approve'}},{dataset:{prAccess:'scope',decision:'deny'}});
 context.api=async()=>{throw Error('Unable to save approval');};context.toast=value=>message=value;
 context.renderPrWriteAccess(run);await buttons[0].onclick();
 assert.ok(buttons.every(button=>!button.disabled));assert.match(area.innerHTML,/>Approve</);
 assert.equal(message,'Unable to save approval');
});
