const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const script=fs.readFileSync('app/static/personal-slack.js','utf8');
function setup(overrides={}) {
  const connection={connected:true,available:true,oauth_configured:true,label:'Alice <personal>',effective_source:'personal',health:'healthy',...overrides};
  const nodes=new Map(),requests=[],redirects=[];
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,disabled:false,innerHTML:'',textContent:'',focus(){this.focused=true;}});return nodes.get(id);};
  const context={state:{pageVersion:1,role:'member'},$:node,document:{activeElement:null,getElementById:id=>node('#'+id),querySelectorAll:()=>[]},esc:x=>String(x).replaceAll('<','&lt;').replaceAll('>','&gt;'),window:{location:{assign:url=>redirects.push(url)}},crypto:{randomUUID:()=> 'fresh'},navigate:async()=>{},showError:error=>{throw error;},confirmSettingsAction:async()=>false,
    api:async(path,options={})=>{requests.push({path,...options});return path.endsWith('/oauth')?{url:'https://slack.com/oauth/v2/authorize'}:connection;}};
  vm.createContext(context);vm.runInContext(script,context);
  return {context,connection,node,requests,redirects};
}
test('members see connected personal identity, escaped account, and immutable private limitation',async()=>{
 const b=setup();await b.context.renderPersonalSlack();const html=b.node('#content').innerHTML;
 assert.match(html,/Alice &lt;personal&gt;/);assert.match(html,/Your personal Slack account/);assert.match(html,/id="personal-slack-disconnect"/);assert.match(html,/never switches to the organization account/);assert.match(html,/Existing shared chats cannot become private/);
});
for(const source of ['organization','none'])test(`absent personal grant displays server effective source ${source}`,async()=>{
 const b=setup({connected:false,effective_source:source,health:'absent'});await b.context.renderPersonalSlack();const html=b.node('#content').innerHTML;
 assert.match(html,/Not connected/);assert.match(html,new RegExp(source==='none'?'No Slack connection':'Organization Slack connection'));assert.doesNotMatch(html,/id="personal-slack-disconnect"/);
});
test('failed personal state stays personal and offers reconnect',async()=>{
 const b=setup({health:'revoked'});await b.context.renderPersonalSlack();const html=b.node('#content').innerHTML;
 assert.match(html,/Needs attention/);assert.match(html,/Your personal Slack account/);assert.match(html,/Reconnect Slack/);
});
test('ineligible sign-ins and missing OAuth configuration explain disabled actions',async()=>{
 const b=setup({connected:false,available:false,oauth_configured:false});await b.context.renderPersonalSlack();const html=b.node('#content').innerHTML;
 assert.match(html,/id="personal-slack-oauth" class="primary" disabled/);assert.match(html,/id="personal-slack-new-chat" disabled/);assert.match(html,/individual workspace account/);assert.match(html,/administrator must configure/);
});
test('OAuth uses requester-owned endpoint and check refreshes authoritative state',async()=>{
 const b=setup();await b.context.renderPersonalSlack();await b.node('#personal-slack-oauth').onclick();
 assert.deepEqual(b.redirects,['https://slack.com/oauth/v2/authorize']);assert.equal(b.requests.at(-1).path,'/api/connections/slack/personal/oauth');assert.equal(b.requests.at(-1).method,'POST');
 await b.node('#personal-slack-check').onclick();assert.deepEqual(b.requests.slice(-2).map(x=>[x.path,x.method]),[['/api/connections/slack/personal/check','POST'],['/api/connections/slack/personal',undefined]]);
 assert.equal(b.node('#personal-slack-result').textContent,'Connection checked.');
});
test('disconnect cancellation performs no mutation; confirmed removal explains fallback and privacy',async()=>{
 const b=setup();await b.context.renderPersonalSlack();await b.node('#personal-slack-disconnect').onclick();assert.equal(b.requests.length,1);
 b.context.confirmSettingsAction=async(title,description)=>{assert.match(description,/organization connection/);assert.match(description,/private sessions stay private/);return true;};
 await b.node('#personal-slack-disconnect').onclick();assert.equal(b.requests[1].method,'DELETE');assert.equal(b.requests[1].path,'/api/connections/slack/personal');assert.equal(b.requests[2].method,undefined);
});
test('check failure is actionable and never calls organization endpoints',async()=>{
 const b=setup();await b.context.renderPersonalSlack();b.context.api=async(path)=>{b.requests.push({path});throw Error('Personal Slack unavailable; retry.');};
 await b.node('#personal-slack-check').onclick();assert.match(b.node('#personal-slack-error').textContent,/retry/);assert.ok(b.requests.every(x=>x.path.startsWith('/api/connections/slack/personal')));
});
test('late loads and OAuth responses cannot overwrite or redirect another page',async()=>{
 const b=setup();let release;b.context.api=()=>new Promise(r=>release=r);const loading=b.context.renderPersonalSlack();b.context.state.pageVersion++;release(b.connection);await loading;assert.equal(b.node('#content').innerHTML,'');
 b.context.api=async()=>b.connection;await b.context.renderPersonalSlack();b.context.api=()=>new Promise(r=>release=r);const oauth=b.node('#personal-slack-oauth').onclick();b.context.state.pageVersion++;release({url:'https://slack.com'});await oauth;assert.deepEqual(b.redirects,[]);
});
test('navigating while disconnect confirmation is open cancels mutation',async()=>{
 const b=setup();await b.context.renderPersonalSlack();b.context.confirmSettingsAction=async()=>{b.context.state.pageVersion++;return true;};await b.node('#personal-slack-disconnect').onclick();assert.equal(b.requests.length,1);
});
test('private shortcut starts blank with a fresh attachment bucket and drops pending request identity',async()=>{
 const b=setup();b.context.state.newDraft={prompt:'old private context',attachment_key:'old'};b.context.state.pendingNew={client_id:'old'};
 let route;b.context.navigate=async view=>route=view;await b.context.startPrivateWebChat();assert.equal(route,'tasks');assert.equal(b.context.state.newDraft.private_session,true);assert.equal(b.context.state.newDraft.prompt,undefined);assert.equal(b.context.state.newDraft.attachment_key,'private-new-fresh');assert.equal(b.context.state.pendingNew,null);
});
const app=fs.readFileSync('app/static/app.js','utf8');
for(const [checked,locked,expected] of [[false,false,false],[true,false,true],[false,true,true]])test(`new-session payload private=${checked}, inherited lock=${locked}`,async()=>{
 const fields=Object.fromEntries(Object.entries({'#prompt':'Read Slack','#repo':'','#project-environment':'auto','#mode':'demo','#new-model':'demo','#new-harness':''}).map(([key,value])=>[key,{value}]));
 fields['#private-session']={checked};fields['#task-form']={querySelector:()=>({isConnected:true})};let body;
 const context={state:{sending:new Set(),attachments:{ids:()=>[],clear(){},lock(){}},newDraft:{private_locked:locked}},$:id=>fields[id],document:{querySelectorAll:()=>[]},crypto:{randomUUID:()=> 'new-request'},api:async(path,options)=>{body=JSON.parse(options.body);return {id:'run'};},refreshRuns:async()=>{},openRun:async()=>{},autoSize(){},toast(e){throw Error(e);}};
 vm.createContext(context);vm.runInContext(app.slice(app.indexOf('async function submitTask('),app.indexOf('\nasync function openRun(')),context);await context.submitTask({preventDefault(){}});assert.equal(body.private_session,expected);assert.equal(body.client_id,'new-request');
});
test('private retry locks inherited context and both rendering paths show privacy',()=>{
 assert.match(app,/private_locked:!!run.private_owner_id/);assert.equal((app.match(/This session stays private and cannot be shared or delegated/g)||[]).length,2);
 const panel=fs.readFileSync('app/static/workspace-panel.js','utf8');
 const make=panel.slice(panel.indexOf('function make('),panel.indexOf('function open(kind'));
 let notice='';const c={run:{private_owner_id:'alice'},toast:x=>notice=x};vm.createContext(c);vm.runInContext(make,c);assert.equal(c.make('chat',{chatId:'restored'}),null);assert.match(notice,/unavailable for private/);
});
test('new private and shared composers bind separate attachment stores',()=>{
 const keys=[];const context={state:{newDraft:{},config:{}},bindSkillEditor:x=>x,bindInlineSkillPicker:()=>({}),bindAttachments:(input,form,key)=>{keys.push(key);return {};},autoSize(){}};
 vm.createContext(context);vm.runInContext(app.slice(app.indexOf('function bindComposer('),app.indexOf('\nasync function navigate(')),context);
 const input={id:'prompt',addEventListener(){}};
 context.bindComposer(input,{});context.state.newDraft.private_session=true;context.bindComposer(input,{});
 context.state.newDraft={attachment_key:'fresh',private_session:true};context.bindComposer(input,{});
 assert.deepEqual(keys,['new','new:private','fresh:private']);
});
