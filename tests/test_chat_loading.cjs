const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('./helpers/ui-vm.cjs');
const script=readFileSync('app/static/app.js','utf8');
const runId='a'.repeat(32),otherId='b'.repeat(32);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((done,fail)=>{resolve=done;reject=fail;});return {promise,resolve,reject};}
function load(context,start,end){vm.runInContext(script.slice(script.indexOf(start),script.indexOf(end)),context);}

function loadingFixture(hash='#run='+runId){
  const requests=[],rendered=[],navigations=[],errors=[],registrations=[],pagehide=[],nodes=new Map();
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',hidden:false,addEventListener(){},dispatchEvent(){}});return nodes.get(selector);};
  const state={runs:[],folders:[],role:'member',pageVersion:0,runsRefresh:0,expandedParents:new Set(),config:{},organization:{}};
  const context={state,URLSearchParams,AbortController,clearTimeout,location:{hash,search:''},$:node,document:{hidden:true,querySelector:()=>null,querySelectorAll:()=>[],modelContext:{registerTool:(tool,options)=>registrations.push({tool,options})}},
    window:{addEventListener:(type,handler)=>{if(type==='pagehide')pagehide.push(handler);}},computer:{close(){}},savedFiles:{reset(){}},toast(){},
    history:{replaceState:(_,__,value)=>context.location.hash=value},settingsViews:new Set(['settings']),
    api:path=>{const request={path,...deferred()};requests.push(request);return request.promise;},
    applyUserSession:session=>{state.authenticated=session.authenticated;state.csrf=session.csrf;},restoreSessionFolderView:()=>{state.folderViewRestored=true;},renderSidebar(){},setView(){},
    showError:error=>errors.push(error),sessionRows:rows=>rows,sessionTitle:run=>run.display_title||'Session',esc:String,
    renderChat:run=>{state.chatRun=run;rendered.push(run.id);},
    navigate:async view=>{state.pageVersion++;state.selected=null;navigations.push(view);}};
  vm.createContext(context);vm.runInContext(readFileSync('app/static/credentials.js','utf8'),context);
  load(context,'function stopStream()','function sessionTitle(');
  load(context,'function sessionRows(','function modelName(');
  load(context,'async function refreshRuns(','function restoreSessionScope(');
  load(context,'async function openRun(','function renderChat(');
  load(context,'async function boot(','function registerWebMCP(');
  load(context,'function registerWebMCP(',"\nmatchMedia('(max-width:850px)')");
  const request=path=>requests.findLast(item=>item.path===path);
  return {context,state,requests,rendered,navigations,errors,registrations,pagehide,node,request};
}

test('authenticated startup overlaps configuration, organization, sidebar and selected conversation',async()=>{
  const f=loadingFixture(),boot=f.context.boot();
  assert.deepEqual(f.requests.map(item=>item.path),['/api/session'],'authentication remains the request boundary');
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  assert.deepEqual(f.requests.slice(1).map(item=>item.path),[
    '/api/config','/api/organization','/api/runs?scope=mine&view=sidebar&focus='+runId,'/api/session-folders','/api/runs/'+runId+'?activity=summary']);
  assert.match(f.node('#content').innerHTML,/Loading conversation/);
  f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true});await flush();
  assert.deepEqual(f.rendered,[],'model controls still require configuration');
  f.request('/api/config').resolve({model:'fixture'});await boot;
  assert.deepEqual(f.rendered,[runId],'neither organization nor session list delays the conversation');
  assert.equal(f.state.sessionSearchLoading,true);
  f.request('/api/organization').resolve({name:'Fixture'});
  f.request('/api/runs?scope=mine&view=sidebar&focus='+runId).resolve([{id:runId}]);
  f.request('/api/session-folders').resolve({folders:[]});await flush();
  assert.equal(f.state.sessionSearchLoading,false);assert.equal(f.state.selected,runId);
  assert.deepEqual(f.errors,[]);
});

test('home can finish loading while the independent sidebar and organization are still pending',async()=>{
  const f=loadingFixture('#tasks'),boot=f.context.boot();
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  f.request('/api/config').resolve({model:'fixture'});await boot;
  assert.deepEqual(f.navigations,['tasks']);assert.equal(f.state.sessionSearchLoading,true);
  assert(f.request('/api/organization'));assert(f.requests.find(item=>item.path.startsWith('/api/runs?')));
});

for(const hash of ['#settings','#run='+otherId,'#run='+runId])test(`route change to ${hash} while authentication is pending retains shared startup without reopening the route`,async()=>{
  const f=loadingFixture(),boot=f.context.boot();
  f.state.pageVersion++;f.state.selected=hash==='#settings'?null:hash.slice(5);f.context.location.hash=hash;
  f.node('#content').innerHTML='New route';
  f.request('/api/session').resolve({authenticated:true,local:true,csrf:'authenticated-token'});await flush();
  assert.equal(f.state.authenticated,true);assert.equal(f.state.csrf,'authenticated-token');assert.equal(f.state.folderViewRestored,true);
  assert(f.request('/api/config'));assert(f.request('/api/organization'));assert(f.request('/api/session-folders'));
  const sidebar=f.requests.find(item=>item.path.startsWith('/api/runs?'));assert(sidebar);
  assert.equal(f.requests.some(item=>item.path.endsWith('?activity=summary')),false,'shared startup must not reopen the newer chat mount');
  f.request('/api/config').resolve({model:'fixture'});
  f.request('/api/organization').resolve({name:'Fixture'});sidebar.resolve([{id:runId}]);
  f.request('/api/session-folders').resolve({folders:[]});await boot;await flush();
  assert.equal(f.state.config.model,'fixture');assert.equal(f.state.organization.name,'Fixture');assert.equal(f.state.sessionSearchLoading,false);
  assert.equal(f.node('#content').innerHTML,'New route');assert.equal(f.context.location.hash,hash);
  assert.deepEqual(f.navigations,[]);assert.deepEqual(f.rendered,[]);assert.deepEqual(f.errors,[]);
});

test('a delayed home startup cannot navigate away from a chat selected while configuration loads',async()=>{
  const f=loadingFixture('#tasks'),boot=f.context.boot();
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  const opening=f.context.openRun(otherId);
  f.request('/api/config').resolve({model:'fixture'});
  f.request('/api/runs/'+otherId+'?activity=summary').resolve({id:otherId,chat_enabled:true});
  await Promise.all([boot,opening]);
  assert.deepEqual(f.navigations,[]);assert.deepEqual(f.rendered,[otherId]);
});

test('a slow conversation response cannot replace a newer selection or its loading state',async()=>{
  const f=loadingFixture();f.state.runs=[{id:runId},{id:otherId}];
  const first=f.context.openRun(runId),second=f.context.openRun(otherId);
  f.request('/api/runs/'+otherId+'?activity=summary').resolve({id:otherId,chat_enabled:true});await second;
  f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true});await first;
  assert.deepEqual(f.rendered,[otherId]);assert.equal(f.state.selected,otherId);
  assert.equal(f.context.location.hash,'#run='+otherId);
});

test('a recent conversation renders before the network settles and refreshes without remounting',async()=>{
  const f=loadingFixture();f.state.runs=[{id:runId}];
  f.state.navigationCache={get:()=>({id:runId,chat_enabled:true,messages:['saved']})};
  const updates=[];f.context.updateChat=run=>updates.push(run);
  const opening=f.context.openRun(runId);
  assert.deepEqual(f.rendered,[runId],'cached content is visible synchronously');
  assert.doesNotMatch(f.node('#content').innerHTML,/Loading conversation/);
  f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true,messages:['new']});
  await opening;assert.deepEqual(f.rendered,[runId],'the composer is not remounted');
  assert.deepEqual(updates[0].messages,['new']);
});

test('a cached conversation refresh cannot overwrite a newer stream refresh or route',async()=>{
  for(const supersede of ['stream','route']){
    const f=loadingFixture();f.state.runs=[{id:runId}];
    f.state.navigationCache={get:()=>({id:runId,chat_enabled:true})};
    const updates=[];f.context.updateChat=run=>updates.push(run);
    const opening=f.context.openRun(runId);
    if(supersede==='stream')f.state.chatRefresh++;else f.state.pageVersion++;
    f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true});await opening;
    assert.equal(updates.length,0);
  }
});

test('an initial linked-chat failure keeps its route Retry and retries only the conversation',async()=>{
  const f=loadingFixture(),boot=f.context.boot();
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  f.request('/api/config').resolve({model:'fixture'});
  f.request('/api/runs/'+runId+'?activity=summary').reject(Error('Conversation unavailable'));await boot;
  assert.match(f.node('#content').innerHTML,/data-retry-chat/);
  const retry=f.node('#content [data-retry-chat]').onclick();
  f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true});await retry;
  assert.deepEqual(f.rendered,[runId]);assert.equal(f.requests.filter(item=>item.path==='/api/session').length,1);
});

test('configuration failure can retry authenticated startup instead of reusing a rejected configuration promise',async()=>{
  const f=loadingFixture(),boot=f.context.boot();
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true});
  f.request('/api/config').reject(Error('Configuration unavailable'));await boot;
  assert.match(f.node('#content').innerHTML,/data-retry-chat/);
  const retry=f.node('#content [data-retry-chat]').onclick();
  assert.equal(f.requests.filter(item=>item.path==='/api/config').length,1,'retry waits for authentication again');
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  f.request('/api/config').resolve({model:'recovered'});
  f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true});await retry;
  assert.deepEqual(f.rendered,[runId]);assert.equal(f.state.config.model,'recovered');assert.equal(f.state.configError,null);
  assert.equal(f.registrations.length,2);assert.equal(f.pagehide.length,1,'startup Retry cannot duplicate tool registrations or document teardown');
});

for(const stage of ['auth','config','detail','detail-error','deleted','config-error'])test(`authenticated WebMCP registration is independent of ${stage} route settlement`,async()=>{
  const f=loadingFixture(stage==='config'?'#tasks':'#run='+runId),boot=f.context.boot();
  const replace=()=>{f.state.pageVersion++;f.state.selected=otherId;f.context.location.hash='#run='+otherId;f.node('#content').innerHTML='New route';};
  if(stage==='auth')replace();
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  assert.deepEqual(f.registrations.map(item=>item.tool.name),['list_workspace_tasks','create_demo_task'],'tools register before configuration or chat detail settles');
  if(stage==='config')replace();
  if(stage==='config-error')f.request('/api/config').reject(Error('Configuration failed'));else f.request('/api/config').resolve({model:'fixture'});
  if(stage==='detail'){await flush();replace();}
  const detail=f.request('/api/runs/'+runId+'?activity=summary');
  if(detail){if(stage==='detail-error'||stage==='deleted')detail.reject(Object.assign(Error('Detail failed'),{status:stage==='deleted'?404:503}));else detail.resolve({id:runId,chat_enabled:true});}
  await boot;assert.equal(f.registrations.length,2);assert.equal(f.pagehide.length,1);
  if(['auth','config','detail'].includes(stage))assert.equal(f.node('#content').innerHTML,'New route');
  f.context.registerWebMCP();assert.equal(f.registrations.length,2);
  f.pagehide[0]();assert(f.registrations.every(item=>item.options.signal.aborted));
});

test('signed-out startup never registers WebMCP tools or dispatches authenticated data reads',async()=>{
  const f=loadingFixture(),boot=f.context.boot();f.request('/api/session').resolve({authenticated:false,local:true});await boot;
  f.context.registerWebMCP();assert.equal(f.registrations.length,0);assert.equal(f.pagehide.length,0);
  assert.deepEqual(f.requests.map(item=>item.path),['/api/session']);
});

function nestedRows(id,prefix){return [{id:prefix+'root',children:[{id:prefix+'parent',parent_run_id:prefix+'root',children:[{id:prefix+'worker',parent_run_id:prefix+'parent',children:[{id,parent_run_id:prefix+'worker'}]}]}]}];}
test('delayed sidebar topology reveals all selected ancestors once and preserves manual collapse',async()=>{
  const f=loadingFixture(),boot=f.context.boot();f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  f.request('/api/config').resolve({});f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true,parent_run_id:'worker',workflow_root_id:'root'});await boot;
  assert(f.state.expandedParents.has('worker'));
  f.state.expandedParents.delete('worker'); // A manual choice made while the rest of the tree is still loading.
  f.request('/api/runs?scope=mine&view=sidebar&focus='+runId).resolve(nestedRows(runId,''));f.request('/api/session-folders').resolve({folders:[]});await flush();
  assert(f.state.expandedParents.has('root'));assert(f.state.expandedParents.has('parent'));assert(!f.state.expandedParents.has('worker'));
  f.state.expandedParents.delete('root');const refresh=f.context.refreshRuns();
  f.request('/api/runs?scope=mine&view=sidebar&focus='+runId).resolve(nestedRows(runId,''));f.request('/api/session-folders').resolve({folders:[]});await refresh;
  assert(!f.state.expandedParents.has('root'),'later polling must preserve the user collapse');
});

test('a delayed list for a replaced route requests the missing current focus and reveals only its ancestors',async()=>{
  const f=loadingFixture(),boot=f.context.boot();f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  f.request('/api/config').resolve({});f.request('/api/runs/'+runId+'?activity=summary').resolve({id:runId,chat_enabled:true,parent_run_id:'a-worker',workflow_root_id:'a-root'});await boot;
  const next=f.context.openRun(otherId);f.request('/api/runs/'+otherId+'?activity=summary').resolve({id:otherId,chat_enabled:true,parent_run_id:'b-worker',workflow_root_id:'b-root'});await next;
  f.state.expandedParents.clear();
  f.request('/api/runs?scope=mine&view=sidebar&focus='+runId).resolve(nestedRows(runId,'a-'));f.request('/api/session-folders').resolve({folders:[]});await flush();
  const focused=f.request('/api/runs?scope=mine&view=sidebar&focus='+otherId);assert(focused,'the current selection must not be omitted by an older focus response');
  focused.resolve(nestedRows(otherId,'b-'));f.request('/api/session-folders').resolve({folders:[]});await flush();
  assert(f.state.expandedParents.has('b-root'));assert(f.state.expandedParents.has('b-parent'));
  assert(!f.state.expandedParents.has('a-root'));assert(!f.state.expandedParents.has('a-parent'));assert.equal(f.state.selected,otherId);
  assert.equal(f.requests.filter(item=>item.path==='/api/runs?scope=mine&view=sidebar&focus='+otherId).length,1);
});

function installHome(f){
  f.state.newDraft={};Object.assign(f.context,{modelPreferenceSave:Promise.resolve(),environmentOptions:()=>'',harnessPicker:()=>'',modelPicker:()=>'',providerNames:{},submitTask(){},bindHarnessPicker(){},bindComposer(){},Event});
  load(f.context,'async function navigate(','async function refreshRuns(');load(f.context,'async function renderHome(','async function submitTask(');
}
test('real home startup reuses shared config and renders while the sole sidebar and organization requests remain pending',async()=>{
  const f=loadingFixture('#tasks');installHome(f);const boot=f.context.boot();
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();f.request('/api/config').resolve({model:'initial'});await flush();
  assert.equal(f.requests.filter(item=>item.path==='/api/config').length,1);
  assert.equal(f.requests.filter(item=>item.path.startsWith('/api/runs?')).length,1);
  f.request('/api/connections').resolve([]);f.request('/api/environments').resolve([]);await flush();
  assert.match(f.node('#content').innerHTML,/What are we working on/);await boot;assert.equal(f.state.sessionSearchLoading,true);
  const saved=deferred();f.context.modelPreferenceSave=saved.promise;const later=f.context.navigate('tasks');await flush();
  assert.equal(f.requests.filter(item=>item.path==='/api/config').length,1,'later home freshness waits for preference writes');
  saved.resolve();await flush();assert.equal(f.requests.filter(item=>item.path==='/api/config').length,2);
  f.request('/api/config').resolve({model:'latest preference'});f.request('/api/connections').resolve([]);f.request('/api/environments').resolve([]);await flush();
  assert.equal(f.state.config.model,'latest preference');await later;
  assert.equal(f.requests.filter(item=>item.path.startsWith('/api/runs?')).length,2,'later navigation refreshes the sidebar independently');
});

for(const hash of ['#tasks','#run='+runId])test(`a stale startup configuration error cannot overwrite a newer view from ${hash}`,async()=>{
  const f=loadingFixture(hash),boot=f.context.boot();
  f.request('/api/session').resolve({authenticated:true,local:true});await flush();
  f.state.pageVersion++;f.state.selected=otherId;f.node('#content').innerHTML='New view';
  f.request('/api/config').reject(Error('Old configuration failure'));await boot;
  assert.equal(f.node('#content').innerHTML,'New view');assert.equal(f.state.selected,otherId);
});

function historyFixture(){
  const requests=[],synced=[],summaries=[];
  const state={selected:runId,pageVersion:1,chatRun:{id:runId,events:[{id:1,kind:'chat'}],deferred_activity:['10','20'],loaded_activity:[]}};
  const context={state,URLSearchParams,$:()=>({}),renderMarkdown:String,copyText(){},savedFiles:{decorate(){}},
    MoyaiActivity:{sync:(_,run)=>synced.push(run)},renderActivitySummary:run=>summaries.push(run),
    api:path=>{const request={path,...deferred()};requests.push(request);return request.promise;}};
  vm.createContext(context);load(context,'async function loadActivity(','function syncEventTimeline(');
  return {context,state,requests,synced,summaries};
}

test('work history is requested on demand, deduplicated, paginated against one snapshot and merged with live events',async()=>{
  const f=historyFixture();assert.equal(f.requests.length,0);
  const first=f.context.loadActivity('10'),same=f.context.loadActivity('10');assert.equal(f.requests.length,1);
  assert.equal(f.requests[0].path,`/api/runs/${runId}/activity?message_id=10&after=0`);
  f.requests[0].resolve({events:[{id:2,kind:'tool'}],until:100,has_more:true,next_after:2});await flush();
  assert.equal(f.requests.length,2);
  assert.equal(f.requests[1].path,`/api/runs/${runId}/activity?message_id=10&after=2&until=100`);
  f.state.chatRun.events.push({id:110,kind:'tool',message:'Arrived live'});
  f.requests[1].resolve({events:[{id:2,kind:'tool'},{id:3,kind:'tool'}],until:100,has_more:false,next_after:3});
  await Promise.all([first,same]);
  assert.deepEqual(Array.from(f.state.chatRun.events,event=>event.id),[1,2,3,110]);
  assert.deepEqual(Array.from(f.state.chatRun.loaded_activity),['10']);
  assert.equal(f.synced.length,1);assert.equal(f.summaries.length,1);assert.equal(f.state.activityRequests.size,0);
  await f.context.loadActivity('10');assert.equal(f.requests.length,2,'completed history stays cached for this mount');
});

test('a failed history page does not mark partial history complete and the next expansion retries',async()=>{
  const f=historyFixture(),loading=f.context.loadActivity('10');
  f.requests[0].resolve({events:[{id:2}],until:100,has_more:true,next_after:2});await flush();
  f.requests[1].reject(Error('Temporarily unavailable'));await assert.rejects(loading,/Temporarily unavailable/);
  assert.deepEqual(f.state.chatRun.loaded_activity,[]);assert.equal(f.state.activityRequests.size,0);
  assert.deepEqual(f.state.chatRun.events,[{id:1,kind:'chat'}]);
  const retry=f.context.loadActivity('10');assert.match(f.requests[2].path,/after=0$/);
  f.requests[2].resolve({events:[{id:2},{id:3}],until:120,has_more:false});await retry;
  assert.deepEqual(Array.from(f.state.chatRun.loaded_activity),['10']);
});

test('nonadvancing history pages fail instead of looping forever',async()=>{
  const f=historyFixture(),loading=f.context.loadActivity('10');
  f.requests[0].resolve({events:[],until:100,has_more:true,next_after:0});
  await assert.rejects(loading,/Please retry/);assert.equal(f.requests.length,1);assert.equal(f.state.activityRequests.size,0);
});

for(const reopenSame of [false,true])test(`history completion after ${reopenSame?'reopening the same session':'changing sessions'} cannot mutate the replacement mount`,async()=>{
  const f=historyFixture(),loading=f.context.loadAllActivity();
  f.state.pageVersion++;f.state.selected=reopenSame?runId:otherId;
  const replacement={id:f.state.selected,events:[{id:200}],loaded_activity:[]};f.state.chatRun=replacement;
  f.requests[0].resolve({events:[{id:2}],until:100,has_more:false});await loading;
  assert.equal(f.state.chatRun,replacement);assert.deepEqual(replacement.events,[{id:200}]);
  assert.deepEqual(replacement.loaded_activity,[]);assert.equal(f.synced.length,0);
  assert.equal(f.requests.length,1,'loading the Activity tab stops before fetching another turn');
});

function transcriptFixture(){
  const nodes=new Map(),markdown=[];
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{dataset:{},innerHTML:'',scrollHeight:800,scrollTop:400,clientHeight:400,querySelectorAll:()=>[]});return nodes.get(selector);};
  const state={selected:runId};
  const context={state,$:node,syncChatComposer(){},MoyaiQueue:{presentation:run=>({transcript:run.messages})},MoyaiActivity:{sync(){}},
    savedFiles:{sync(){}},esc:String,messageAttachments:()=>'',renderMarkdown:text=>{markdown.push(text);return text;},copyText(){},loadActivity(){},
    modelName:String,updateChatStatus(){},renderCredentialRequests(){},renderApprovals(){},renderPrWriteAccess(){},renderSlackContext(){},renderAgentDetails(){}};
  vm.createContext(context);
  for(const file of ['icons','skill-icons','skill-composer'])vm.runInContext(readFileSync(`app/static/${file}.js`,'utf8'),context);
  load(context,'function updateChat(run','function renderLiveWork(');
  return {context,state,markdown,node};
}

test('appending a message reuses existing Markdown templates and active turn history stays loaded after completion',()=>{
  const f=transcriptFixture();
  const skill={reference:'personal:team',name:'team',scope:'personal',icon:'team'};
  const run={id:runId,mode:'modal',events:[],messages:[{id:1,role:'user',status:'running',content:'First /personal:team question',skill_mentions:[skill]},
    {id:2,role:'assistant',status:'completed',content:'First answer'}],loaded_activity:['1']};
  f.context.updateChat(structuredClone(run),true);assert.deepEqual(f.markdown,['First answer']);
  assert.match(f.node('#conversation').innerHTML,/data-skill-reference="personal:team"/);
  assert.match(f.node('#conversation').innerHTML,/data-skill-icon="team"/);
  run.messages[0].status='completed';run.messages.push({id:3,role:'user',status:'running',content:'Next question'},
    {id:4,role:'assistant',status:'completed',content:'Next answer'});
  run.deferred_activity=['1'];run.loaded_activity=['3'];f.context.updateChat(structuredClone(run));
  assert.deepEqual(f.markdown,['First answer','Next answer'],'unchanged assistant content is not parsed again');
  assert.match(f.node('#conversation').innerHTML,/data-skill-icon="team"/);
  assert.deepEqual(Array.from(f.state.chatRun.loaded_activity).sort(),['1','3'],'finishing a previously loaded live turn cannot create a lazy placeholder');
  run.messages[3].content='Updated next answer';f.context.updateChat(structuredClone(run));
  assert.deepEqual(f.markdown,['First answer','Next answer','Updated next answer']);
  assert.match(f.node('#conversation').innerHTML,/Updated next answer/);
  skill.icon='video';f.context.updateChat(structuredClone(run));
  assert.match(f.node('#conversation').innerHTML,/data-skill-icon="video"/);
  assert.doesNotMatch(f.node('#conversation').innerHTML,/data-skill-icon="team"/);
  assert.deepEqual(f.markdown,['First answer','Next answer','Updated next answer'],'metadata-only changes invalidate user templates without reparsing assistant Markdown');
  assert.equal(run.messages[0].content,'First /personal:team question');
});

test('the summary cursor keeps earlier streamed goal events from overriding an authoritative detail goal',()=>{
  const f=transcriptFixture();
  f.state.chatRun={id:runId,events:[{id:50,data:{phase:'goal',goal_version:1,goal:{status:'old'}}}],loaded_activity:[]};
  const run={id:runId,messages:[],events:[{id:1,kind:'chat'}],activity_cursor:100,goal:{status:'current'}};
  f.context.updateChat(run);assert.equal(f.state.chatRun.goal.status,'current');
  f.state.chatRun.events.push({id:101,data:{phase:'goal',goal_version:1,goal:{status:'streamed'}}});
  f.context.updateChat({...run,events:[{id:1,kind:'chat'}],goal:{status:'current'}});
  assert.equal(f.state.chatRun.goal.status,'streamed','an event after the snapshot still wins');
});
