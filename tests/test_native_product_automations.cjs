// Source/server-component checks only. They do not replace actual-app browser proof.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
let server, data, ui, triggers, helpers;
const root=path.resolve(__dirname,'..');
test.before(async()=>{
  const {createServer}=await import('vite');
  server=await createServer({configFile:path.join(root,'native/web/vite.config.ts'),optimizeDeps:{noDiscovery:true,include:[]},server:{middlewareMode:true},appType:'custom'});
  data=await server.ssrLoadModule('/product-automation-data.ts');
  ui=await server.ssrLoadModule('/product-automations.tsx');
  helpers=await server.ssrLoadModule('/automation-helpers.js');
  triggers=helpers;
});
test.after(async()=>{await server?.close();});
const timing=(changes={})=>({frequency:'weekdays',time:'09:00',weekday:1,timezone:'America/Los_Angeles',cron:'',run_at:null,...changes});
const definition=(changes={})=>({name:'Weekly review',prompt:'Review the shared project notes.',model:'enabled-model',mode:'modal',harness:'hermes',repo_url:'',github_repository_id:null,environment_id:'none',plugins:[],metadata:{},queue_events:false,max_runs_per_hour:50,self_management:{instructions:false,schedule:false},triggers:[{id:'original-trigger',schedule:timing()}],...changes});
const binding=(changes={})=>({core:'pydantic-v1',generation:2,state:'paused',sync_state:'ready',synced_generation:2,source_revision:7,last_error:'',...changes});
const preview=(changes={})=>({id:'a'.repeat(32),owner_id:'viewer',revision:7,definition:definition(),legacy_paused:true,native:binding(),blockers:[],supported:true,activation:'explicit_owner_action_required',execution_visibility:'shared',sharing_notice:'Shared workspace resources.',overlap:'ALLOW_ALL',catchup_seconds:900,capabilities:{update:true,migrate:false,enable:true,pause:false},...changes});
const render=(Component,props)=>renderToStaticMarkup(React.createElement(Component,props));

test('automation helpers preserve defaults and calendar summaries',()=>{
  assert.equal(triggers.automationDefaultSchedule().frequency,'weekdays');
  assert.match(helpers.automationTiming(timing()),/weekdays/i);
  assert.deepEqual(triggers.automationTriggers(definition()),definition().triggers);
});

const examples=()=>['hourly','daily','weekdays','weekly','cron','once'].map(frequency=>definition({triggers:[{id:'stable-'+frequency,schedule:timing({frequency,cron:frequency==='cron'?'*/15 9-17 * * 1-5':'',run_at:frequency==='once'?'2099-10-22T16:00:00Z':null})}]}));
test('all six calendar types retain the exact Timing payload fields',()=>{
  for(const example of examples()){data.validateAutomation(example);assert.deepEqual(Object.keys(example.triggers[0].schedule).sort(),['frequency','time','weekday','timezone','cron','run_at'].sort());}
});
const python=path.join(root,'.venv/bin/python');
test('all six calendar types conform to canonical Python Timing and Definition',{skip:!fs.existsSync(python)&&'Native Python environment is not installed in this frontend-only job'},()=>{
  const result=spawnSync(python,['-c','import json,sys; from app.automation_definitions import Definition; rows=json.load(sys.stdin); [Definition.model_validate(row) for row in rows]; print(len(rows))'],{cwd:root,input:JSON.stringify(examples()),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);assert.equal(result.stdout.trim(),'6');
});

test('one-time UTC conversion is exact across browser-local DST boundaries and rejects impossible dates',()=>{
  assert.equal(data.onceUtcInstant('2026-03-08T02:30'),'2026-03-08T02:30:00.000Z');
  assert.equal(data.onceUtcInstant('2026-11-01T01:30:45'),'2026-11-01T01:30:45.000Z');
  assert.equal(data.onceUtcInput('2026-11-01T01:30:45-07:00'),'2026-11-01T08:30:45');
  assert.throws(()=>data.onceUtcInstant('2026-02-30T12:00'),/valid one-time/);
  assert.throws(()=>data.onceUtcInstant('2026-10-01T25:00'),/valid one-time/);
  assert.throws(()=>data.validateAutomation(definition({triggers:[{id:'past',schedule:timing({frequency:'once',run_at:'2000-01-01T00:00:00Z'})}]})),/future/);
});

test('calendar validation rejects invalid IANA zones, cron ranges and duplicate trigger identities',()=>{
  assert.throws(()=>data.validateAutomation(definition({triggers:[{id:'a',schedule:timing({timezone:'Not/AZone'})}]})),/IANA/);
  for(const cron of ['60 * * * *','0 9 * * MON','*/0 * * * *','0 9 * *','0 9 * * 6-1'])assert.throws(()=>data.validateAutomation(definition({triggers:[{id:'a',schedule:timing({frequency:'cron',cron})}]})),/cron|Cron/);
  assert.throws(()=>data.validateAutomation(definition({triggers:[{id:'same',schedule:timing()},{id:'same',schedule:timing()}]})),/unique/);
});

test('saved event and execution settings remain intact while the expanded editor exposes them',()=>{
  const original=definition({plugins:['github'],repo_url:'https://github.com/BerriAI/moyai',environment_id:'auto',metadata:{destination:'Existing channel'},self_management:{instructions:true,schedule:false},triggers:[{id:'old-event',event:{provider:'github',event:'push',repository:'BerriAI/moyai'}},{id:'old-calendar',schedule:timing()}]});
  const before=structuredClone(original);data.validateAutomation(original);assert.deepEqual(original,before);
  const html=render(ui.DefinitionEditor,{existing:original,models:['enabled-model'],defaultModel:'enabled-model',eventChoices:{github:[['push','Push']]},busy:false,onCancel(){},async onSave(){}});
  for(const label of ['Connections and tools','Repository','Metadata','Self-management','Maximum runs per hour','Queue overlapping event runs','Event type'])assert.ok(html.includes(label),label);
  assert.match(html,/Existing channel/);assert.match(html,/value="BerriAI\/moyai"/);
  assert.doesNotMatch(html,/Run now|Enable schedule|cannot be converted|awaiting migration support/);
});

test('server capabilities, readiness, revision and generation all gate activation',()=>{
  assert.equal(data.automationActions(preview()).enable,true);
  for(const changed of [
    {capabilities:undefined},{supported:false},{blockers:['connection_mapping_required']},
    {native:binding({sync_state:'pending'})},{native:binding({sync_state:'blocked'})},
    {native:binding({synced_generation:1})},{native:binding({source_revision:6})},
    {native:binding({state:'deleted'})},{native:null},
  ])assert.equal(data.automationActions(preview(changed)).enable,false,JSON.stringify(changed));
  assert.equal(data.automationActions(preview({native:null,capabilities:{migrate:true}})).migrate,true);
  assert.equal(data.automationActions(preview({native:null,blockers:['event_trigger_adapter_required'],capabilities:{migrate:true}})).migrate,false);
  assert.equal(data.automationActions(preview({native:binding({state:'active',sync_state:'blocked'}),capabilities:{pause:true}})).pause,true);
});

test('pending or stale bindings never claim scheduled/retired completion',()=>{
  assert.equal(data.automationStatus(preview({native:binding({state:'active',sync_state:'pending'})})),'Sync pending');
  assert.equal(data.automationStatus(preview({native:binding({state:'deleted',sync_state:'pending'})})),'Sync pending');
  assert.equal(data.automationStatus(preview({native:binding({state:'active',source_revision:6})})),'Needs review');
  assert.equal(data.automationStatus(preview({native:binding({state:'active'})})),'Enabled');
});

test('mutation retries reuse their request key while changed intent and revision receive a new one',()=>{
  const keys=new data.AutomationRequestKeys(),body={revision:7,activate:false};
  const first=keys.forIntent('/product/automations/id/migrate',body);
  assert.match(first,/^[A-Za-z0-9_-]{8,80}$/);assert.equal(keys.forIntent('/product/automations/id/migrate',{...body}),first);
  assert.notEqual(keys.forIntent('/product/automations/id/migrate',{revision:8,activate:false}),first);
  assert.notEqual(keys.forIntent('/product/automations/id/enable',{revision:7}),first);
});

test('history reuses canonical deduplication and routes old/new sessions without rewriting IDs',()=>{
  const a={occurrence:'a',created_at:'2026-10-15',engine:'legacy',session_id:'old:id',status:'idle',outcome:'completed'},b={...a,occurrence:'b',created_at:'2026-10-16',engine:'pydantic-v1',session_id:'new:id'};
  assert.deepEqual(helpers.automationHistoryMerge([a],[a,b]),[b,a]);
  assert.equal(data.historySessionHash(a),'#run=old%3Aid');assert.equal(data.historySessionHash(b),'#new%3Aid');
  assert.equal(data.historySessionHash({...a,engine:'unknown'}),undefined);assert.equal(data.historySessionHash({...a,session_id:null}),undefined);
});

test('real shadcn editor escapes instructions and makes non-save form actions inert',()=>{
  const html=render(ui.DefinitionEditor,{existing:definition({name:'<script>bad</script>',prompt:'<img src=x onerror=alert(1)>'}),models:['enabled-model'],defaultModel:'enabled-model',busy:false,onCancel(){},async onSave(){}});
  assert.doesNotMatch(html,/<script>|<img src=x/);assert.match(html,/&lt;img/);assert.match(html,/Save paused/);assert.match(html,/shared with your workspace/);
  const buttons=[...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)];
  for(const [,attrs,body]of buttons)if(!body.includes('Save paused'))assert.match(attrs,/type="button"/);
});

test('readiness and list show backend blockers and real state without inventing unsupported events or run times',()=>{
  const p=preview({supported:false,blockers:['connection_mapping_required','repository_bootstrap_adapter_required','once_already_consumed:old'],native:null});
  const html=render(ui.AutomationReadiness,{preview:p});assert.match(html,/Blocked/);assert.match(html,/explicit migration mapping/);assert.match(html,/already run/);assert.doesNotMatch(html,/Next run:/);
  const list=render(ui.ProductAutomationList,{rows:[{id:p.id,revision:7,name:'<img onerror=x>',legacy_paused:true,native:null,triggers:[{id:'event',event:{provider:'slack',event:'message.posted'}}]}],onReview(){}});
  assert.match(list,/Slack/);assert.match(list,/Review migration/);assert.match(list,/Not migrated/);assert.doesNotMatch(list,/<img|Migration not supported/);
});

test('changing event kind clears incompatible filters while preserving provider identity and unrelated fields',()=>{
  const original={provider:'slack',event:'message.posted',channel_id:'C12345678',text_contains:'alert',text_starts_with:'bug:',sender_type:'bot',include_thread_replies:true};
  const reaction=data.changeAutomationEvent(original,'reaction.added');
  assert.equal(reaction.channel_id,original.channel_id);assert.equal(reaction.text_contains,'');assert.equal(reaction.text_starts_with,'');
  assert.equal(reaction.sender_type,'any');assert.equal(reaction.include_thread_replies,false);assert.equal(original.sender_type,'bot');
  assert.equal(data.eventFieldVisible(reaction,'sender_type'),false);assert.equal(data.eventFieldVisible(reaction,'reaction'),true);
  const push=data.changeAutomationEvent({provider:'github',event:'check_run',repository:'owner/repo',repository_id:123,conclusion:'failure',label:'bug',branch:'main'},'push');
  assert.equal(push.conclusion,'');assert.equal(push.label,'');assert.equal(push.branch,'main');assert.equal(push.repository_id,123);
});

test('repository edits discard stale permanent IDs without mutating the saved definition',()=>{
  const original=definition({repo_url:'https://github.com/owner/old',github_repository_id:42});
  assert.equal(data.changeAutomationRepository(original,original.repo_url).github_repository_id,42);
  assert.equal(data.changeAutomationRepository(original,'https://github.com/owner/new').github_repository_id,null);
  assert.equal(original.github_repository_id,42);
  const event={provider:'github',event:'issues',repository:'owner/old',repository_id:42};
  assert.equal(data.changeAutomationEventField(event,'repository','owner/new').repository_id,null);
  assert.equal(data.changeAutomationEventField(event,'label','bug').repository_id,42);
  assert.throws(()=>data.validateAutomation(definition({repo_url:'https://user:secret@github.com/owner/repo'})),/repository URL/);
});

const eventExamples=()=>[
  {provider:'session',event:'message.posted',session_id:'a'.repeat(32),text_contains:'please'},
  {provider:'session',event:'message.posted',session_id:'12345678-1234-1234-1234-123456789abc'},
  {provider:'slack',event:'message.posted',channel_id:'C12345678',sender_type:'human',include_thread_replies:true},
  {provider:'slack',event:'reaction.added',channel_id:'C12345678',reaction:'eyes'},
  {provider:'github',event:'push',repository:'owner/repo',repository_id:42,branch:'main'},
  {provider:'gitlab',event:'pipeline',repository:'group/project',status:'failed'},
  {provider:'linear',event:'issue.created',team_id:'12345678-1234-1234-1234-123456789abc',priority:0},
  {provider:'jira',event:'issue.updated',project:'ENG'},
  {provider:'pylon',event:'issue.created',label:'urgent'},
  {provider:'pagerduty',event:'incident.triggered',service_id:'PABC123'},
  {provider:'webhook',event:'*',payload_pattern:'"priority":\\s*"high"'},
];
test('every source renders the reused field definitions and escapes user-controlled values',()=>{
  for(const event of eventExamples()){
    const html=render(ui.EventFields,{event,choices:[[event.event,'Current event']],disabled:false,onChange(){}});
    assert.match(html,/data-slot="input"/,event.provider);
    if(event.provider==='session')assert.match(html,/maxLength="36"/);
    if(event.provider==='linear')assert.match(html,/value="0"/);
    if(event.event==='reaction.added'){assert.doesNotMatch(html,/Message contains|Sender|Include thread replies/);assert.match(html,/Reaction name/);}
  }
  const html=render(ui.EventFields,{event:{provider:'webhook',event:'<img src=x onerror=x>',payload_pattern:'<script>x</script>'},choices:[],disabled:false,onChange(){}});
  assert.doesNotMatch(html,/<script>|<img src/);assert.match(html,/&lt;script/);
});
test('all event sources, native/original session IDs and advanced fields conform to canonical Python schemas',{skip:!fs.existsSync(python)&&'Native Python environment is not installed'},()=>{
  const definitions=eventExamples().map((event,index)=>definition({triggers:[{id:'event-'+index,event}],plugins:['linear','github','slack','notion'],environment_id:'b'.repeat(32),metadata:{team:'engineering'},self_management:{instructions:true,schedule:true},queue_events:true,max_runs_per_hour:null}));
  const result=spawnSync(python,['-c','import json,sys; from app.automation_definitions import Definition; rows=json.load(sys.stdin); [Definition.model_validate(row) for row in rows]; print(len(rows))'],{cwd:root,input:JSON.stringify(definitions),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);assert.equal(Number(result.stdout.trim()),definitions.length);
});

test('metadata and sample validation preserve inert strings and reject ambiguous or oversized data',()=>{
  assert.deepEqual(data.automationMetadata([{key:' team ',value:'<script>inert</script>'}]),{team:'<script>inert</script>'});
  assert.throws(()=>data.automationMetadata([{key:' ',value:''}]),/key/);
  assert.throws(()=>data.automationMetadata([{key:'team',value:'a'},{key:' team ',value:'b'}]),/different key/);
  assert.deepEqual(data.automationSamplePayload('{"priority":0}'),{priority:0});
  for(const sample of ['null','[]','true','"string"'])assert.throws(()=>data.automationSamplePayload(sample),/JSON object/);
  assert.throws(()=>data.automationSamplePayload(JSON.stringify({value:'ü'.repeat(131073)})),/256 KB/);
});

test('manual runs require current capability and synchronized bindings; a confirmed run gets a new intent key',()=>{
  assert.equal(data.automationActions(preview({capabilities:{run:true}})).run,true);
  for(const patch of [{capabilities:{}},{supported:false},{native:null},{native:binding({synced_generation:1})},{native:binding({source_revision:6})},{native:binding({state:'deleted'})}])
    assert.equal(data.automationActions(preview({capabilities:{run:true},...patch})).run,false);
  const keys=new data.AutomationRequestKeys(),body={revision:7},path='/product/automations/id/run';
  const first=keys.forIntent(path,body);assert.equal(keys.forIntent(path,body),first);
  keys.complete();assert.notEqual(keys.forIntent(path,body),first);
});

test('unadvertised event sources remain readable and saved fields are not silently discarded',()=>{
  const html=render(ui.DefinitionEditor,{existing:definition({triggers:[{id:'old',event:{provider:'new-provider',event:'incident',filter:'keep me'}}]}),models:['enabled-model'],defaultModel:'enabled-model',busy:false,onCancel(){},async onSave(){}});
  assert.match(html,/server did not provide an editor contract/);assert.match(html,/keep me/);
  assert.doesNotMatch(html,/data-slot="input"[^>]*value="incident"/);
});
