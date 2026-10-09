const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
const source=fs.readFileSync('app/static/app.js','utf8');
const functions=source.slice(source.indexOf('function harnessLogo('),source.indexOf('\nfunction setSidebar('));
function setup(){
  const context={state:{config:{harness:'codex',model:'openai/gpt-6-astra',harnesses:[{id:'hermes',name:'Hermes'},{id:'codex',name:'Codex'},{id:'claude-agent-sdk',name:'Claude Code',model_prefix:'anthropic/claude-'}],models:[{id:'openai/gpt-6-astra',name:'Astra',default_harness:'codex'},{id:'anthropic/claude-opus-5-5',name:'Opus',default_harness:'claude-agent-sdk'}]}},esc:s=>String(s),MoyaiProviderLogos:require('../app/static/provider-logos.js')};
  vm.createContext(context);vm.runInContext(functions,context);return context;
}
function picker(value){return {value,innerHTML:'',closest:()=>({querySelector:()=>null}),addEventListener(type,handler){this[type]=handler;}};}
test('new picker displays the automatic model default and retains explicit drafts',()=>{
  const c=setup();assert.match(c.harnessPicker(),/value="" selected>Auto · Codex/);
  assert.match(c.harnessPicker('', 'anthropic/claude-opus-5-5'),/value="" selected>Auto · Claude Code/);
  assert.match(c.harnessPicker('hermes'),/value="hermes" selected/);
  assert.match(c.harnessPicker('claude-agent-sdk'),/value="claude-agent-sdk" selected/);
  assert.match(c.harnessPicker('saved-removed-harness'),/value="saved-removed-harness" selected/);
});
test('model changes update automatic metadata without changing an explicit choice',()=>{
  const c=setup(),harness=picker(''),model=picker('openai/gpt-6-astra');let changes=0;
  c.bindHarnessPicker(harness,model,()=>changes++);
  model.value='anthropic/claude-opus-5-5';model.change();
  assert.match(harness.innerHTML,/value="" selected>Auto · Claude Code/);
  harness.value='hermes';harness.change();
  model.value='openai/gpt-6-astra';model.change();
  assert.match(harness.innerHTML,/value="hermes" selected/);
  assert.doesNotMatch(harness.innerHTML,/value="" selected/);
  assert.equal(changes,3);
});
test('effective defaults follow server configuration, including deployment overrides',()=>{
  const c=setup();c.state.config.models[0].default_harness='hermes';
  assert.match(c.harnessPicker(),/Auto · Hermes/);
  const harness=picker('claude-agent-sdk'),model=picker('openai/gpt-6-astra');
  c.bindHarnessPicker(harness,model,()=>{},false);model.change();
  assert.doesNotMatch(harness.innerHTML,/Auto ·/);
  assert.match(harness.innerHTML,/value="claude-agent-sdk" selected/);
});
test('every harness offers all configured models even with stale provider metadata',()=>{
  const c=setup();assert.equal(c.harnessModels('hermes').length,2);
  for(const harness of ['hermes','claude-agent-sdk','codex','opencode','deepagents','tool-loop','pi']){
    assert.equal(c.harnessModels(harness).length,2);
    const html=c.modelPicker('model','openai/gpt-6-astra',false,harness);
    assert.match(html,/Opus/);assert.match(html,/value="openai\/gpt-6-astra" selected/);
  }
});
test('harness picker shows the selected harness logo and hides it for harnesses without one',()=>{
  const c=setup();
  assert.match(c.harnessPicker('claude-agent-sdk'),/<img class="provider-logo harness-logo"[^>]*src="\/static\/harness-logos\/claude-code.svg"/);
  assert.match(c.harnessPicker('hermes'),/<img class="provider-logo harness-logo"[^>]*hidden>/);
});
test('new-session submission omits automatic selection and preserves an explicit override',async()=>{
  const submission=source.slice(source.indexOf('async function submitTask('),source.indexOf('\nasync function openRun('));
  for(const selected of ['', 'hermes']){
    const fields=Object.fromEntries(Object.entries({'#prompt':'Run a task','#repo':'','#project-environment':'auto','#mode':'modal','#new-model':'openai/gpt-6-astra','#new-harness':selected}).map(([key,value])=>[key,{value}]));
    fields['#task-form']={querySelector:()=>({disabled:false,isConnected:true})};
    let payload;
    const context={state:{sending:new Set(),attachments:{ids:()=>[],lock(){},clear(){}},newDraft:{}},$:id=>fields[id],document:{querySelectorAll:()=>[]},crypto:{randomUUID:()=> 'request-id'},api:async(path,request)=>{assert.equal(path,'/api/runs');payload=JSON.parse(request.body);return {id:'saved-session'};},refreshRuns:async()=>{},openRun:async()=>{},autoSize(){},toast(message){throw Error(message);}};
    vm.createContext(context);vm.runInContext(submission,context);
    await context.submitTask({preventDefault(){}});
    assert.equal(payload.model,'openai/gpt-6-astra');
    if(selected)assert.equal(payload.harness,selected);
    else assert.equal(Object.hasOwn(payload,'harness'),false);
  }
});
