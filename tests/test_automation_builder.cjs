const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
const {randomUUID}=require('node:crypto');
function context(extra={}) {
  const c={Intl,Date,Map,Set,crypto:{randomUUID},state:{pageVersion:1,config:{cloud_ready:true,model:'model',models:[{id:'model',name:'Model'}]}},providerNames:{github:'GitHub'},esc:s=>String(s).replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x])),...extra};
  vm.createContext(c);for(const file of ['automation-triggers.js','automations.js','automation-builder.js'])vm.runInContext(readFileSync('app/static/'+file,'utf8'),c);return c;
}
test('library filters combine ownership, paused status and metadata without changing data',()=>{
  const c=context(),a={can_edit:false,paused:true,owner:'Teammate',definition:{name:'Weekly review',prompt:'Review tickets',metadata:{team:'Platform'}}};
  assert.equal(c.automationMatches(a,{scope:'mine',status:'all',search:''}),false);
  assert.equal(c.automationMatches(a,{scope:'all',status:'paused',search:' platform '}),true);
  assert.equal(c.automationMatches(a,{scope:'all',status:'enabled',search:'platform'}),false);
  assert.equal(c.automationMatches(a,{scope:'all',status:'all',search:'missing'}),false);
});
test('template presets keep a local timezone, fresh IDs and editable copies',()=>{
  const c=context(),t={name:'Weekly',plugins:['github'],schedule:{frequency:'weekly',weekday:2,time:'10:00'}},a=c.automationNewDefinition(t),b=c.automationNewDefinition(t);
  assert.equal(a.triggers[0].schedule.weekday,2);assert.ok(a.triggers[0].schedule.timezone);assert.notEqual(a.triggers[0].id,b.triggers[0].id);
  assert.equal(a.repo_url,'');assert.equal(a.mode,'modal');
  const event={provider:'github',event:'check_run.completed',conclusion:'failure'};
  const d=c.automationNewDefinition({event});d.triggers[0].event.repository='example/repo';assert.equal(event.repository,undefined);
});
test('connection tool descriptions are escaped and unavailable choices are labelled',()=>{
  const c=context(),html=c.automationConnections([{id:'github',connected:false,tools:[{name:'read',description:'<img onerror=alert(1)>'}]}],['github']);
  assert.doesNotMatch(html,/<img/);assert.match(html,/Not connected/);assert.match(html,/checked/);
});
test('metadata rejects duplicate keys instead of silently overwriting a value',()=>{
  const c=context(),form={querySelectorAll:selector=>selector.includes('key')?[{value:'team'},{value:' team '}]:[{value:'one'},{value:'two'}]};
  assert.throws(()=>c.readAutomationMetadata(form),/different key/);
});
test('metadata value validation counts Unicode characters and recovers after correction',()=>{
  const input={value:'🚀'.repeat(16384),setCustomValidity(message){this.message=message;}},button={},add={};
  const item={querySelector:s=>s==='button'?button:input},host={children:[],append(row){this.children.push(row);}};
  const form={querySelector:s=>s==='[data-metadata]'?host:add};
  const c=context({document:{createElement:()=>item}});c.automationMetadata(form,{notes:input.value});
  assert.equal(input.message,'');input.value+='x';input.oninput();assert.match(input.message,/16,384/);
  input.value='Corrected\nnotes';input.oninput();assert.equal(input.message,'');
  assert.match(item.innerHTML,/<textarea name="metadata_value"/);assert.doesNotMatch(item.innerHTML,/maxlength="500"/);
});
function generatorContext({suggest=false,recent=true,description='Every Monday review tickets',fail=false}={}) {
  const requests=[],opened=[],errors={textContent:''},button={},form={elements:{description:{focus(){}}},querySelector:s=>s==='[type=submit]'?button:errors};
  const dialog={open:true,querySelector:()=>form,close(){this.open=false;}};
  const fields={description,model:'chosen-model',recent:recent?'on':null};let failOnce=fail;
  const c=context({FormData:class{get(key){return fields[key];}getAll(){return ['github'];}},api:async(path,options)=>{
    if(path==='/api/connections')return [{id:'github',connected:true,enabled:true}];
    if(path==='/api/runs?scope=mine')return [{display_title:'Review failed builds',prompt:'PRIVATE PROMPT'},{display_title:'Child work',parent_run_id:'parent'},{display_title:'Automated run',agent_label:'Automation'},{prompt:'PRIVATE UNTITLED PROMPT'}];
    requests.push(JSON.parse(options.body));if(failOnce){failOnce=false;throw Error('Connection lost');}return {id:'new-session'};
  },showError:()=>{},refreshRuns:async()=>{},openRun:async id=>opened.push(id),toast:message=>{throw Error(message);}});
  c.automationDialog=()=>dialog;
  return {c,form,dialog,requests,opened,errors,fields,suggest};
}
test('generation starts a real cloud chat with selected tools and reuses its ID after a transport error',async()=>{
  const t=generatorContext({fail:true});await t.c.generateAutomation(false);
  await t.form.onsubmit({preventDefault(){}});assert.equal(t.errors.textContent,'Connection lost');assert.equal(t.dialog.open,true);
  await t.form.onsubmit({preventDefault(){}});
  assert.equal(t.requests.length,2);assert.equal(t.requests[0].client_id,t.requests[1].client_id);
  assert.equal(t.requests[1].mode,'modal');assert.equal(t.requests[1].model,'chosen-model');assert.deepEqual(t.requests[1].plugins,['github']);
  assert.match(t.requests[1].prompt,/save it PAUSED/);assert.match(t.requests[1].prompt,/automation_list/);assert.match(t.requests[1].prompt,/Every Monday review tickets/);
  assert.deepEqual(t.opened,['new-session']);
});
test('suggestions include only recent top-level titles, never prompts or automatic work',async()=>{
  const t=generatorContext({description:''});await t.c.generateAutomation(true);await t.form.onsubmit({preventDefault(){}});
  assert.match(t.requests[0].prompt,/Review failed builds/);assert.doesNotMatch(t.requests[0].prompt,/PRIVATE|Child work|Automated run/);
  assert.match(t.requests[0].prompt,/Do not create or enable anything yet/);
});
test('suggestions require some context when recent titles are not selected',async()=>{
  const t=generatorContext({description:'',recent:false});await t.c.generateAutomation(true);await t.form.onsubmit({preventDefault(){}});
  assert.equal(t.requests.length,0);assert.match(t.errors.textContent,/Describe some recurring work/);
});
