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
test('consent identifies scope, escapes metadata and exposes only allowed transitions',()=>{
 for(const status of ['pending','approved','denied','revoked']){
  const {context,area,run}=setup(status);context.renderPrWriteAccess(run);
  assert.match(area.innerHTML,/org\/repo #7/);assert.match(area.innerHTML,/saved chat only/);
  assert.doesNotMatch(area.innerHTML,/<img|<script>/);
  assert.equal(area.innerHTML.includes('Allow for this chat'),status==='pending');
  assert.equal(area.innerHTML.includes('Revoke access'),status==='approved');
 }
});
test('approval responses cannot replace a newly opened chat',async()=>{
 const {context,area,run,buttons}=setup();buttons.push({dataset:{prAccess:'scope',decision:'approve'}});
 let resolve;context.api=async(path,options)=>options?new Promise(r=>resolve=r):run;context.renderPrWriteAccess(run);
 const pending=buttons[0].onclick();context.state.pageVersion++;context.state.selected='other';
 area.innerHTML='other chat';resolve({status:'approved'});await pending;
 assert.equal(area.innerHTML,'other chat');
});
test('continue prepares an editable follow-up without sending or replacing a draft',()=>{
 const {context,run,input,continuations}=setup('approved');continuations.push({dataset:{prContinue:'7'}});
 context.renderPrWriteAccess(run);continuations[0].onclick();assert.match(input.value,/PR #7/);assert.equal(input.focused,true);
 input.value='my draft';continuations[0].onclick();assert.equal(input.value,'my draft');
});
