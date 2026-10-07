const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const script = readFileSync('app/static/app.js','utf8');
function helpers(){
  const context={state:{selected:'worker-b'},relative:()=> '2m ago'};
  vm.createContext(context);vm.runInContext(readFileSync('app/static/credentials.js','utf8'),context);
  vm.runInContext(script.slice(script.indexOf('const esc ='),script.indexOf('const state ='))+
    script.slice(script.indexOf('function sessionTitle('),script.indexOf('function modelName('))+
    script.slice(script.indexOf('function sidebarGroups('),script.indexOf('function renderSidebar(')),context);
  return context;
}
function scopeHelpers(){
  const element={value:'mine'}, saved=new Map(), errors=[];
  const context={state:{userId:'google:alice',role:'admin',authenticated:true,runsRefresh:0,runs:[]},URLSearchParams,
    location:{hash:''},$:()=>element,localStorage:{getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)},
    renderSidebar:()=>{},showError:error=>errors.push(error.message)};
  vm.createContext(context);vm.runInContext(readFileSync('app/static/credentials.js','utf8'),context);
  vm.runInContext(script.slice(script.indexOf('async function refreshRuns('),script.indexOf('async function renderHome(')),context);
  vm.runInContext(readFileSync('app/static/users.js','utf8'),context);
  return {context,element,saved,errors};
}
test('admin scope defaults to mine and preferences are isolated per user',()=>{
  const {context:c,element,saved}=scopeHelpers();
  c.restoreSessionScope();assert.equal(element.value,'mine');
  saved.set('moyai-session-scope:google:alice','all');
  c.restoreSessionScope();assert.equal(element.value,'all');
  c.state.userId='google:bob';c.restoreSessionScope();assert.equal(element.value,'mine');
  saved.set('moyai-session-scope:google:bob','invalid');c.restoreSessionScope();assert.equal(element.value,'mine');
  c.localStorage.getItem=()=>{throw Error('Denied');};c.restoreSessionScope();assert.equal(element.value,'mine');
});
test('members and signed-out users cannot restore a saved all-sessions preference',()=>{
  for(const session of [{role:'member',authenticated:true},{role:'admin',authenticated:false}]){
    const {context:c,element,saved}=scopeHelpers();Object.assign(c.state,session);
    saved.set('moyai-session-scope:google:alice','all');
    c.restoreSessionScope();
    assert.equal(c.state.sessionScope,'mine');assert.equal(element.value,'mine');
    assert.equal(element.disabled,true);assert.doesNotMatch(element.innerHTML,/value="all"/);
    assert.equal(saved.get('moyai-session-scope:google:alice'),'mine');
    c.localStorage.setItem=()=>{throw Error('Denied');};c.restoreSessionScope();
    assert.equal(c.state.sessionScope,'mine');
  }
});
test('member requests stay personal even if cached state or the picker is set to all',async()=>{
  const {context:c,element}=scopeHelpers(),paths=[];
  c.state.role='member';c.state.sessionScope='all';element.value='all';
  c.api=async path=>{paths.push(path);return path==='/api/session-folders'?{folders:[]}:[];};
  await c.refreshRuns();await c.changeSessionScope();
  assert.equal(c.state.sessionScope,'mine');assert.equal(element.value,'mine');
  for(const path of paths.filter(path=>path.startsWith('/api/runs'))){
    assert.equal(new URL(path,'http://local').searchParams.get('scope'),'mine');
  }
});
test('a refreshed demotion removes all-sessions controls and discards in-flight admin results',async()=>{
  const {context:c,element,saved}=scopeHelpers();
  saved.set('moyai-session-scope:google:alice','all');c.restoreSessionScope();
  assert.equal(element.disabled,false);assert.match(element.innerHTML,/value="all"/);
  let resolve;
  c.api=path=>path==='/api/session-folders'?Promise.resolve({folders:[]}):new Promise(done=>resolve=done);
  const pending=c.refreshRuns();
  c.state.runs=[{id:'unrelated'}];c.state.folders=[{id:'old-folder'}];
  c.applyUserSession({role:'member',authenticated:true,user_id:'google:alice'});
  assert.equal(c.state.runs.length,0);assert.equal(c.state.folders.length,0);
  assert.equal(element.value,'mine');assert.equal(element.disabled,true);
  assert.doesNotMatch(element.innerHTML,/value="all"/);
  resolve([{id:'stale-admin-result'}]);await pending;
  assert.equal(c.state.runs.length,0);
});
test('scope and focus are forwarded and stale list responses cannot replace newer scope',async()=>{
  const {context:c,element}=scopeHelpers(),pending=[];
  c.state.selected='a'.repeat(32);c.restoreSessionScope();
  c.api=path=>path==='/api/session-folders'?Promise.resolve({folders:[]}):new Promise(resolve=>pending.push({path,resolve}));
  const first=c.refreshRuns();
  element.value='all';const second=c.changeSessionScope();
  assert.equal(new URL(pending[0].path,'http://local').searchParams.get('scope'),'mine');
  assert.equal(new URL(pending[0].path,'http://local').searchParams.get('focus'),'a'.repeat(32));
  assert.equal(new URL(pending[1].path,'http://local').searchParams.get('scope'),'all');
  pending[1].resolve([{id:'all-results'}]);await second;
  pending[0].resolve([{id:'stale-results'}]);await first;
  assert.equal(c.state.runs[0].id,'all-results');
});
test('failed filter change clears foreign rows and preserves selected chat',async()=>{
  const {context:c,element,errors}=scopeHelpers();c.restoreSessionScope();
  c.state.runs=[{id:'foreign'}];c.state.selected='shared-chat';element.value='mine';
  c.api=async()=>{throw Error('List unavailable');};
  c.localStorage.setItem=()=>{throw Error('Denied');};
  await c.changeSessionScope();
  assert.equal(c.state.runs.length,0);assert.equal(c.state.selected,'shared-chat');
  assert.deepEqual(errors,['List unavailable']);
});

test('archive view forwards its scope and discards a late active listing',async()=>{
  const {context:c}=scopeHelpers(),pending=[];
  c.document={querySelectorAll:()=>[]};
  c.$=()=>({setAttribute:()=>{}});
  c.api=path=>path==='/api/session-folders'?Promise.resolve({folders:[]}):new Promise(resolve=>pending.push({path,resolve}));
  const active=c.refreshRuns(),archived=c.changeSessionView(true);
  assert.equal(new URL(pending[1].path,'http://local').searchParams.get('archived'),'true');
  pending[1].resolve([{id:'archived'}]);await archived;
  pending[0].resolve([{id:'stale-active'}]);await active;
  assert.equal(c.state.runs[0].id,'archived');
});

test('switching between empty active and archived lists refreshes the empty message',()=>{
  const c=helpers(),elements=new Map();
  for(const id of ['#session-search','#task-count','#workspace-name','#session-list'])
    elements.set(id,{value:'',innerHTML:'',scrollTop:0});
  Object.assign(c,{document:{activeElement:null},$:id=>elements.get(id)});
  Object.assign(c.state,{runs:[],folders:[],organization:{},expandedParents:new Set(),closedFolders:new Set()});
  vm.runInContext(script.slice(script.indexOf('function renderSidebar('),script.indexOf('function setView(')),c);
  c.renderSidebar();assert.match(elements.get('#session-list').innerHTML,/conversations will appear/);
  c.state.sessionArchived=true;c.renderSidebar();
  assert.match(elements.get('#session-list').innerHTML,/No archived sessions/);
});

const runs=[{id:'parent',prompt:'Benchmark models',children:[
  {id:'worker-a',agent_label:'Cases 1–20',status:'running'},
  {id:'worker-b',agent_label:'Cases 21–40',status:'idle'},
]},{id:'other',prompt:'Other session',children:[]}];

test('searching a worker keeps its parent and excludes unrelated siblings',()=>{
  const h=helpers(),found=h.sidebarGroups(runs,'21–40');
  assert.equal(found.length,1);
  assert.equal(found[0].id,'parent');
  assert.equal(found[0].children.length,1);
  assert.equal(found[0].children[0].id,'worker-b');
  assert.equal(found[0].totalChildren,2);
  assert.equal(h.sidebarGroups(runs,'benchmark')[0].children.length,2);
});
test('child rows use their assignment label and expose selection and live status',()=>{
  const h=helpers(),html=h.sidebarRow(runs[0].children[1],true);
  assert.match(html,/aria-current="page"/);
  assert.match(html,/Cases 21–40/);
  assert.match(html,/data-run="worker-b"/);
  assert.match(html,/session-indicator/);assert.match(html,/Ready/);
  assert.match(h.sidebarRow(runs[0].children[0],true),/session-spinner/);
  assert.match(h.sidebarRow(runs[0].children[0],true),/Working now/);
});
test('assignment labels cannot inject sidebar markup',()=>{
  const h=helpers();
  const html=h.sidebarRow({id:'worker-c',agent_label:'<img src=x onerror="bad()">',status:'idle'},true);
  assert.doesNotMatch(html,/<img/);
  assert.match(html,/&lt;img/);
});

test('folders group parent sessions once and searching a worker keeps its folder',()=>{
  const h=helpers(),filed=[{...runs[0],folder_id:'today'},runs[1]];
  const folders=[{id:'today',name:'Today'},{id:'empty',name:'Research'}];
  const all=h.sidebarSections(filed,folders,'');
  assert.equal(all.folders.length,2);
  assert.equal(all.folders[0].groups[0].id,'parent');
  assert.equal(all.folders[1].groups.length,0);
  assert.equal(all.recent.length,1);
  assert.equal(all.recent[0].id,'other');
  const found=h.sidebarSections(filed,folders,'21–40');
  assert.equal(found.folders.length,1);
  assert.equal(found.folders[0].groups[0].children[0].id,'worker-b');
  assert.equal(found.recent.length,0);
  assert.equal(h.sidebarSections(filed,folders,'today').folders[0].groups[0].children.length,2);
  assert.equal(h.sidebarSections(filed,folders,'unmatched').folders.length,0);
});
test('a removed or stale folder leaves its sessions in Recent',()=>{
  const h=helpers(),sections=h.sidebarSections([{...runs[0],folder_id:'removed'}],[],'');
  assert.equal(sections.folders.length,0);
  assert.equal(sections.recent[0].id,'parent');
});
