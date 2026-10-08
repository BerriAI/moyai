const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('./helpers/ui-vm.cjs');
const source = readFileSync('app/static/session-actions.js', 'utf8');
function harness() {
  const notices = [], calls = [];
  const state = {runs:[{id:'one'},{id:'two'}],runsRefresh:1,chatRefresh:1,chatRun:{id:'one',archived:false}};
  const context = {state,renderSidebar:()=>{},toast:message=>notices.push(message),
    refreshRuns:async()=>calls.push('refresh'),api:async(path,options)=>calls.push({path,options})};
  vm.createContext(context);vm.runInContext(readFileSync('app/static/credentials.js','utf8'),context);vm.runInContext(source,context);
  return {context,state,notices,calls};
}
function menuHarness(chat) {
  const h=harness(),c=h.context,nodes=new Map();let button,pending;
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{focus(){},setAttribute(){},addEventListener(){},closest:()=>null,getBoundingClientRect:()=>({right:300,bottom:50})});return nodes.get(selector);};
  const menu={innerHTML:'',style:{},offsetWidth:200,offsetHeight:120,querySelector:node,matches:()=>false,
    hidePopover(){},showPopover(){},insertAdjacentHTML(_,html){this.innerHTML+=html;}};
  const detail={id:'one',chat_enabled:chat,archived:false};
  Object.assign(h.state,{selected:'one',runs:[{...detail},{id:'two'}],chatRun:chat?{...detail}:null});
  Object.assign(c,{document:{createElement:()=>node('header')},$:selector=>selector==='#session-actions'?menu:{append:value=>button=value},
    innerWidth:800,innerHeight:600,showError:error=>h.notices.push(error.message)});
  const folders=readFileSync('app/static/session-folders.js','utf8');
  vm.runInContext(folders.slice(folders.indexOf('function showSessionActions('),folders.indexOf('function renameSession(')),c);
  const change=c.changeSessionArchive;c.changeSessionArchive=run=>pending=change(run);
  c.bindSessionHeaderActions(detail);
  return {...h,menu,detail,button,archive:()=>{node('[data-archive-session]').onclick();return pending;}};
}
test('sidebar and header menus anchor shadcn positioning to their actual trigger',()=>{
  const {context:c,menu,detail,button}=menuHarness(true);
  let anchor;
  menu.showPopover=options=>anchor=options.source;
  c.showSessionActions(detail,button);
  assert.equal(anchor,button);
  anchor=null;
  button.onclick();
  assert.equal(anchor,button);
  // Viewport collisions and keyboard focus are covered by shadcn_ui.cjs.
});
for(const chat of [false,true])for(const origin of ['header','sidebar'])test(`${chat?'chat':'legacy'} header tracks archive and restore started from the ${origin}`,async()=>{
  const {context:c,state,menu,button,archive,calls}=menuHarness(chat);
  if(origin==='header')button.onclick();else c.showSessionActions(state.runs[0],button);
  assert.match(menu.innerHTML,/Archive session/);
  await archive();assert.equal(state.runs.some(run=>run.id==='one'),false);
  button.onclick();assert.match(menu.innerHTML,/Restore session/);
  await archive();button.onclick();assert.match(menu.innerHTML,/Archive session/);
  assert.deepEqual(calls.filter(call=>call.options).map(call=>JSON.parse(call.options.body)),[{archived:true},{archived:false}]);
});
test('archive invalidates earlier reads and updates only the selected session metadata', async () => {
  const {context:c,state,notices,calls} = harness();
  await c.changeSessionArchive({id:'one',archived:false});
  assert.equal(calls[0].path,'/api/runs/one/archive');
  assert.deepEqual(JSON.parse(calls[0].options.body),{archived:true});
  assert.deepEqual(state.runs.map(run=>run.id),['two']);
  assert.equal(state.runsRefresh,2);assert.equal(state.chatRefresh,2);
  assert.equal(state.sessionEdits,1);
  assert.equal(state.chatRun.archived,true);assert.equal(state.sessionMutation,null);
  assert.match(notices[0],/Ask Moyai to find it/);
});
test('rejected archive retains the row and permits retry without false success', async () => {
  const {context:c,state,notices,detail,menu,button,archive} = menuHarness(true);
  c.api=async()=>{throw Error('Save unavailable');};
  await assert.rejects(c.changeSessionArchive(detail),/Save unavailable/);
  assert.equal(state.runs.length,2);assert.equal(state.chatRun.archived,false);
  assert.equal(detail.archived,false);button.onclick();assert.match(menu.innerHTML,/Archive session/);
  assert.equal(state.runsRefresh,1);assert.equal(state.sessionMutation,null);assert.equal(notices.length,0);
  c.api=async()=>{};await archive();button.onclick();assert.match(menu.innerHTML,/Restore session/);
});
test('a pending restore keeps its original intent and never changes a different open session or header', async () => {
  const {context:c,state,notices,calls} = menuHarness(true);let finish;
  c.api=async(path,options)=>{calls.push({path,options});await new Promise(resolve=>finish=resolve);};
  const item={id:'one',archived:true},pending=c.changeSessionArchive(item);
  state.chatRun={id:'two',archived:true};item.archived=false;
  c.bindSessionHeaderActions({id:'two',archived:true});
  await c.changeSessionArchive({id:'one',archived:true});
  assert.equal(calls.length,1);finish();await pending;
  assert.equal(state.chatRun.archived,true);
  assert.equal(state.sessionHeaderRun.archived,true);
  assert.deepEqual(JSON.parse(calls[0].options.body),{archived:false});
  assert.match(notices[0],/restored/);
});

function deletionHarness() {
  const h=harness(),nodes=new Map();
  for(const selector of ['form','[data-cancel]','[data-stop-session]','.folder-error','submit'])
    nodes.set(selector,{disabled:false,hidden:selector==='[data-stop-session]',focus(){this.focused=true;}});
  const buttons=['[data-cancel]','[data-stop-session]','submit'].map(selector=>nodes.get(selector));
  const dialog={open:false,querySelector:selector=>nodes.get(selector),querySelectorAll:()=>buttons,
    showModal(){this.open=true;},close(){this.open=false;}};
  Object.assign(h.context,{$:()=>dialog,esc:value=>value,sessionTitle:run=>run.id,
    navigate:async view=>h.calls.push({navigate:view}),showError:error=>h.notices.push(error.message)});
  h.state.selected='one';
  h.context.deleteSessionDialog({id:'one'});
  return {...h,dialog,nodes,buttons,submit:()=>nodes.get('form').onsubmit({preventDefault(){}})};
}
test('failed deletion keeps the session and dialog, then permits a successful retry',async()=>{
  const {context:c,state,dialog,nodes,buttons,submit,calls,notices}=deletionHarness();
  assert.equal(nodes.get('[data-cancel]').focused,true);
  c.api=async()=>{throw Error('Delete unavailable');};
  await submit();
  assert.equal(dialog.open,true);assert.equal(state.runs.length,2);
  assert.equal(nodes.get('.folder-error').textContent,'Delete unavailable');
  assert.equal(nodes.get('[data-stop-session]').hidden,true);
  assert.equal(buttons.every(button=>!button.disabled),true);assert.equal(notices.length,0);
  c.api=async()=>{};await submit();
  assert.equal(dialog.open,false);assert.deepEqual(state.runs.map(run=>run.id),['two']);
  assert.equal(calls.at(-1).navigate,'tasks');assert.equal(notices.at(-1),'Session deleted.');
});
test('busy deletion offers the existing stop flow before retrying deletion',async()=>{
  const {context:c,state,dialog,nodes,submit,calls}=deletionHarness();
  c.api=async()=>{throw Object.assign(Error('Wait for cleanup'),{status:409});};
  await submit();
  assert.equal(nodes.get('[data-stop-session]').hidden,false);
  c.api=async(path,options)=>calls.push({path,options});
  await nodes.get('[data-stop-session]').onclick();
  assert.equal(calls[0].path,'/api/runs/one/cancel');
  assert.equal(calls[0].options.method,'POST');
  assert.match(nodes.get('.folder-error').textContent,/Stop requested/);
  assert.equal(dialog.open,true);assert.equal(state.runs.length,2);
});
test('delayed deletion never navigates away from a newly selected session',async()=>{
  const {context:c,state,dialog,submit,calls}=deletionHarness();let finish;
  c.api=()=>new Promise(resolve=>finish=resolve);
  const pending=submit();
  state.selected='two';state.activeParentId='two';
  finish();await pending;
  assert.equal(dialog.open,false);assert.equal(state.selected,'two');
  assert.deepEqual(calls,['refresh']);
});

test('archive while opening a session rereads its metadata before rendering the header',async()=>{
  const app=readFileSync('app/static/app.js','utf8');let finish,rendered,reads=0;
  const state={runs:[{id:'one'}],pageVersion:0,expandedParents:new Set()};
  const c={state,stopStream(){},refreshRuns:async()=>{},document:{hidden:true,querySelector:()=>null},
    setView(){},sessionTitle:()=>'',history:{replaceState(){}},renderChat:run=>rendered=run,
    api:async()=>{if(++reads===1)return new Promise(resolve=>finish=resolve);return {id:'one',chat_enabled:true,archived:true};}};
  vm.createContext(c);vm.runInContext(readFileSync('app/static/credentials.js','utf8'),c);
  vm.runInContext(app.slice(app.indexOf('async function openRun('),app.indexOf('function renderChat(')),c);
  const pending=c.openRun('one');state.sessionEdits=1;
  finish({id:'one',chat_enabled:true,archived:false});await pending;
  assert.equal(reads,2);assert.equal(rendered.archived,true);
});

function availabilityHarness(){
  const h=harness(),c=h.context,app=readFileSync('app/static/app.js','utf8');
  const content={innerHTML:'Old conversation'},source={close(){this.closed=true;}};
  Object.assign(h.state,{pageVersion:1,selected:'one',source,drafts:{one:'Unsent reply'}});
  Object.assign(c,{document:{querySelector:()=>null},history:{replaceState(){}},$:()=>content,computer:{close(){}},savedFiles:{reset(){}},clearTimeout(){},
    navigate:async view=>{c.stopStream();h.state.selected=null;h.state.pageVersion++;h.calls.push({navigate:view});}});
  vm.runInContext(app.slice(app.indexOf('function stopStream()'),app.indexOf('function sessionTitle('))+
    app.slice(app.indexOf('async function openRun('),app.indexOf('function renderChat('))+
    app.slice(app.indexOf('async function refreshChat('),app.indexOf('function eventHTML(')),c);
  return {...h,content,source};
}
test('leaving a session releases its header metadata',()=>{
  const {context:c,state}=availabilityHarness();
  state.sessionHeaderRun={id:'one',archived:true};
  c.stopStream();assert.equal(state.sessionHeaderRun,null);
});
for(const method of ['openRun','refreshChat']){
  test(`${method} clears a confirmed unavailable session and returns home`,async()=>{
    const {context:c,state,content,source,calls}=availabilityHarness();
    c.api=async()=>{throw Object.assign(Error('Session not found.'),{status:404});};
    await c[method]('one');
    assert.equal(source.closed,true);assert.equal(state.selected,null);assert.equal(state.chatRun,null);
    assert.match(content.innerHTML,/no longer available/);assert.deepEqual(calls,[{navigate:'tasks'}]);
    assert.equal(state.drafts.one,'Unsent reply');
  });
  test(`${method} ignores a late missing-session response after navigation`,async()=>{
    const {context:c,state,content,calls}=availabilityHarness();let reject;
    c.api=()=>new Promise((_,fail)=>reject=fail);
    const pending=c[method]('one');
    state.pageVersion++;state.selected='two';content.innerHTML='New conversation';
    reject(Object.assign(Error('Session not found.'),{status:404}));await pending;
    assert.equal(state.selected,'two');assert.equal(content.innerHTML,'New conversation');assert.deepEqual(calls,[]);
  });
  test(`${method} does not treat a temporary error as deletion`,async()=>{
    const {context:c,content,calls}=availabilityHarness();
    c.api=async()=>{throw Object.assign(Error('Temporarily unavailable'),{status:503});};
    await assert.rejects(c[method]('one'),/Temporarily unavailable/);
    assert.equal(content.innerHTML,'Old conversation');assert.deepEqual(calls,[]);
  });
}

test('legacy task streams also close on the terminal deletion marker',async()=>{
  const {context:c,state,content}=availabilityHarness();
  Object.assign(c,{document:{hidden:true,querySelector:()=>null},setView(){},sessionTitle:()=>'',esc:value=>value,
    history:{replaceState(){}},terminal:new Set(['completed']),renderApprovals(){},renderPrWriteAccess(){},bindSessionHeaderActions(){},eventHTML:()=>'',statusLabel:value=>value,
    savedFiles:{reset(){},sync(){}},showError:error=>{throw error;},
    EventSource:class{constructor(){this.handlers={};}addEventListener(name,handler){this.handlers[name]=handler;}close(){this.closed=true;}},
    api:async()=>({id:'one',mode:'demo',status:'running',events:[],plugins:[]})});
  state.expandedParents=new Set();
  await c.openRun('one');const source=state.source;
  await source.handlers.deleted();
  assert.equal(source.closed,true);assert.equal(state.selected,null);
  assert.match(content.innerHTML,/no longer available/);
});
