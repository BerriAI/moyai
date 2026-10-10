const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const swarm=require('../app/static/swarm-space.js');
const id=n=>n.toString(16).padStart(32,'0');
const run=(extra={})=>({id:id(1),status:'waiting_children',harness:'claude-agent-sdk',model:'anthropic/claude',agents:{groups:[]},events:[],...extra});
test('space is the actual deduplicated recursive session hierarchy, including harness/model',()=>{
 const child={id:id(2),agent_label:'Build',status:'running',harness:'codex',model:'openai/new',active_model:'openai/current',children:[{id:id(3),agent_label:'Review',status:'idle',harness:'hermes',model:'openai/new'}]};
 child.children.push({id:id(1)});
 const nodes=swarm.members(run({agents:{groups:[{children:[child]},{children:[child,{id:'bad-id'}]}]}}));
 assert.deepEqual(nodes.map(n=>[n.id,n.parentId,n.depth]),[[id(1),null,0],[id(2),id(1),1],[id(3),id(2),2]]);
 assert.equal(nodes[1].active_model,'openai/current');assert.equal(nodes[2].harness,'hermes');
 assert.equal(swarm.members(run()).length,1);assert.deepEqual(swarm.members({id:'unknown'}),[]);
});
test('bounded graph prioritizes attention and active work; overflow stays in the complete session tree',()=>{
 const nodes=[{id:id(1)},...Array.from({length:20},(_,i)=>({id:id(i+2),status:i===19?'awaiting_approval':i===18?'running':'idle'}))];
 const graph=swarm.layout(nodes,4);
 assert.equal(graph.length,5);assert.equal(graph[1].id,id(21));assert.equal(graph[2].id,id(20));
 assert.equal(nodes.length,21);assert.equal(new Set(graph.map(node=>`${node.x}:${node.y}`)).size,5);
 for(const node of graph){assert.ok(node.x>0&&node.x<1);assert.ok(node.y>0&&node.y<1);}
});
test('only a valid server deadline produces a countdown and pause never extends it',()=>{
 const now=Date.parse('2026-10-10T12:00:00Z'),ends_at='2026-10-10T12:30:00Z';
 const active=run({swarm:{status:'active',budget_seconds:1800,ends_at,round:2}});
 assert.equal(swarm.clock(active,now).timing,'Up to 30m 0s left');
 const paused={...active,swarm:{...active.swarm,status:'paused'}};
 assert.equal(swarm.clock(paused,now).label,'Paused');assert.equal(swarm.clock(paused,now+600000).remaining,1200);
 assert.equal(swarm.clock({...active,swarm:{...active.swarm,ends_at:null}},now).remaining,null);
 assert.equal(swarm.clock({...active,swarm:{...active.swarm,ends_at:'invalid'}},now).timing,'30m 0s maximum');
 assert.equal(swarm.clock(active,now+1900000).label,'Wrapping up');
 assert.equal(swarm.clock({...active,status:'failed'},now).label,'Needs attention');
 assert.equal(swarm.clock(run()),null);
});
test('receipts expose saved public activity and skip processing placeholders',()=>{
 const events=[{id:1,kind:'agents',message:'Research returned findings',data:{}},{id:2,kind:'chat',message:'Not a tool receipt'},{id:3,kind:'tool',message:'Processing',data:{phase:'processing'}}];
 assert.deepEqual(swarm.receipt(run({events})),{id:1,text:'Research returned findings',kind:'agents'});
 assert.equal(swarm.receipt(run()),null);
});
test('node controls use the recorded harness, accessible state, and escaped public strings',()=>{
 global.MoyaiProviderLogos=require('../app/static/provider-logos.js');
 const html=swarm.nodeHTML({id:id(2),label:'<script>Build</script>',status:'failed',harness:'hermes',model:'model',x:.5,y:.5},{selectedId:id(2)});
 assert.match(html,/hermes.png/);assert.match(html,/aria-pressed="true"/);assert.match(html,/Failed/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 delete global.MoyaiProviderLogos;
});
test('view switching retains the composer and disposes its canvas, timer, and observer',()=>{
 const elements=new Map(),get=selector=>elements.get(selector)||elements.set(selector,{textContent:'',hidden:false,focus(){this.focused=true;}}).get(selector);
 let click,canvasDisposals=0,timerDisposals=0,observerDisposals=0,nodePaints=0;
 const host={hidden:false,clientWidth:900,dataset:{},querySelector:get,addEventListener(type,fn){click=fn;},removeEventListener(type,fn){assert.equal(fn,click);},contains:()=>true};
 const transcript={hidden:false},composer={value:'Keep this draft',focus(){this.focused=true;}},container={classList:{toggle(){},remove(){}},querySelector:selector=>selector==='#conversation'?transcript:composer};
 const context={console,MoyaiUI:{render(){}},MoyaiRegions:{sync(){nodePaints++;}},MoyaiSpace:{mount(){return ()=>canvasDisposals++;}},MoyaiActivity:{current:()=>({headline:'Working'})},document:{querySelectorAll:()=>[]},ResizeObserver:class{observe(){}disconnect(){observerDisposals++;}},setInterval:()=>7,clearInterval:id=>{assert.equal(id,7);timerDisposals++;}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('app/static/swarm-space.js','utf8'),context);
 const initial=run({swarm:{status:'active',budget_seconds:1800,ends_at:'2026-10-10T12:30:00Z'}});
 const controller=context.MoyaiSwarm.create({host,layout:container,run:initial});
 assert.equal(host.hidden,false);assert.equal(transcript.hidden,true);assert.equal(composer.value,'Keep this draft');
 controller.setView('chat',{focus:true});assert.equal(transcript.hidden,false);assert.equal(host.hidden,true);assert.equal(composer.focused,true);
 const paints=nodePaints;controller.update(initial);assert.equal(nodePaints,paints);
 controller.dispose();controller.update({...initial,status:'failed'});assert.equal(nodePaints,paints);
 assert.equal(canvasDisposals,1);assert.equal(timerDisposals,1);assert.equal(observerDisposals,1);
});
test('new-session payload preserves normal chat and includes only the selected swarm budget',async()=>{
 const source=fs.readFileSync('app/static/app.js','utf8');
 const submission=source.slice(source.indexOf('async function submitTask('),source.indexOf('\nasync function openRun('));
 for(const mode of ['chat','swarm']){
  const fields=Object.fromEntries(Object.entries({'#prompt':'Build a prototype','#repo':'','#project-environment':'auto','#mode':'modal','#new-model':'openai/model','#new-harness':'codex','#swarm-budget':'3600'}).map(([key,value])=>[key,{value}]));let payload;
  const c={stopStream(){},setView(){},sessionTitle:()=>'New session',renderSessionSubmission(){},showError(){},state:{pageVersion:0,sending:new Set(),attachments:{ids:()=>[],lock(){},clear(){}},newDraft:{session_mode:mode}},$:id=>fields[id],document:{querySelectorAll:()=>[]},crypto:{randomUUID:()=> 'request-id'},api:async(path,options)=>{payload=JSON.parse(options.body);return {id:id(1)};},refreshRuns:async()=>{},openRun:async()=>{},toast(message){throw Error(message);}};
  vm.createContext(c);vm.runInContext(submission,c);await c.submitTask({preventDefault(){}});
  assert.equal(payload.client_id,'request-id');assert.equal(payload.harness,'codex');assert.equal(payload.mode,'modal');
  if(mode==='swarm')assert.deepEqual(payload.swarm,{budget_seconds:3600});else assert.equal(Object.hasOwn(payload,'swarm'),false);
 }
});
test('only active missions accept follow-ups; paused and blocked resume inside original deadline and round limit',()=>{
 const now=Date.parse('2026-10-10T12:00:00Z'),base=run({swarm:{status:'active',ends_at:'2026-10-10T12:30:00Z',round:2}});
 assert.equal(swarm.canSend(base,now),true);assert.equal(swarm.canResume(base,now),false);
 for(const status of ['paused','blocked','stopped','expired']){
  const item={...base,swarm:{...base.swarm,status}};
  assert.equal(swarm.canSend(item,now),false);
  assert.equal(swarm.canResume(item,now),['paused','blocked'].includes(status));
  assert.match(swarm.composerNote(item,now),/draft stays here/);
  assert.equal(swarm.canResume(item,now+1900000),false);
  assert.match(swarm.composerNote(item,now+1900000),/Start a new session/);
 }
 assert.equal(swarm.canResume({...base,swarm:{...base.swarm,status:'paused',round:25}},now),false);
 assert.equal(swarm.canSend(base,now+1900000),false);
 assert.equal(swarm.canSend(run(),now),true);
});
test('swarm controls merge compact API responses, refresh details, and ignore responses after navigation',async()=>{
 const source=fs.readFileSync('app/static/app.js','utf8');
 const binding=source.slice(source.indexOf("  $('#swarm-pause')?.addEventListener"),source.indexOf("  $('#workspace-panel-toggle').addEventListener"));
 for(const navigate of [false,true]){
  let handler;const calls=[],button={disabled:false,addEventListener(type,callback){handler=callback;}};
  const compact={id:id(1),status:'cancelled',swarm:{status:'paused'}};
  const c={id:id(1),$:()=>button,state:{pageVersion:1,selected:id(1),chatRun:run({swarm:{status:'active'}})},api:async()=>{if(navigate)c.state.pageVersion++;return compact;},updateChatStatus:next=>{assert.equal(next.messages,undefined);calls.push('merge');},refreshChat:async()=>calls.push('refresh'),syncSwarmControls:()=>calls.push('controls'),toast:message=>assert.fail(message)};
  vm.createContext(c);vm.runInContext(binding,c);await handler();
  assert.deepEqual(calls,navigate?[]:['merge','refresh','controls']);
  assert.equal(c.state.swarmControlPending,null);
 }
});

test('response previews choose public prose, retain saving answers, and never substitute tools or private reasoning',()=>{
 const older={id:8,role:'assistant',content:'Previous answer',status:'completed',response_to_id:7,created_at:'2026-10-10T12:00:00Z'};
 const current=run({active_message_id:9,messages:[older,{id:9,role:'user',content:'Next question',status:'running'}],events:[
  {id:10,kind:'message',message:'I am comparing the results.',created_at:'2026-10-10T12:01:00Z',data:{phase:'commentary',turn_id:9}},
  {id:11,kind:'message',message:'Do not show processing',data:{phase:'processing'}},
  {id:12,kind:'message',message:'Do not show private reasoning',data:{phase:'reasoning'}},
  {id:13,kind:'tool',message:'Do not show tool output',data:{output:'private'}},
  {id:14,kind:'status',message:'Do not show focus as a reply',data:{phase:'focus'}},
 ]});
 assert.equal(swarm.latestResponse(current).text,'I am comparing the results.');
 assert.equal(swarm.latestResponse(current).kind,'update');
 current.messages.push({id:-9,role:'assistant',content:'The actual completed answer',status:'saving',created_at:'2026-10-10T12:02:00Z'});
 assert.equal(swarm.latestResponse(current).id,-9);assert.equal(swarm.latestResponse(current).kind,'reply');
 current.messages.push({id:15,role:'assistant',content:'Superseded answer',status:'steered',created_at:'2026-10-10T12:03:00Z'});
 assert.equal(swarm.latestResponse(current).text,'The actual completed answer');
 assert.equal(swarm.latestResponse(run({summary:'Stale previous answer',events:[{kind:'error',message:'Private diagnostic'}]})),null);
 assert.equal(swarm.latestResponse(run({messages:[{role:'assistant',content:'The task failed before replying.',status:'failed'}]})).kind,'error');
});

test('reply controls expose escaped bounded excerpts and full-conversation action; empty and retry states are honest',()=>{
 const node={id:id(2),label:'<Agent>',harness:'codex'};
 const html=swarm.responseHTML(node,{response:{kind:'reply',text:'<script>bad()</script> '+ 'x'.repeat(600)}});
 assert.match(html,/data-swarm-agent=/);assert.match(html,/Read reply/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>|x{300}/);
 assert.match(swarm.responseHTML(node),/No reply yet/);
 assert.match(swarm.responseHTML(node,{loading:true}),/Loading reply/);
 assert.match(swarm.responseHTML(node,{error:'retry'}),/data-swarm-retry=/);
 assert.match(swarm.responseHTML(node,{error:'unavailable'}),/Reply unavailable/);
 const nodeMarkup=swarm.nodeHTML({...node,x:.5,y:.5,status:'running'},{harnessName:()=>'<Codex>'});
 assert.match(nodeMarkup,/<span class="swarm-node-harness">&lt;Codex&gt;<\/span>/);
});

const flush=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const child=(n,status='running',updated_at='version1')=>({id:id(n),status,updated_at});
const snapshot=(n,extra={})=>({id:id(n),status:'idle',active:false,messages:[{id:n,role:'assistant',content:`Reply ${n}`,status:'completed'}],events:[],...extra});

test('child responses use at most three concurrent snapshots, never overlap, and stop refreshing terminal agents',async()=>{
 let time=0;const calls=[];
 const cache=swarm.responseCache({now:()=>time,api:path=>{const pending=deferred();calls.push({path,...pending});return pending.promise;}});
 const nodes=[2,3,4,5].map(n=>child(n));cache.sync(nodes);cache.poll();cache.poll();await flush();
 assert.equal(calls.length,3);assert.ok(calls.every(call=>call.path.endsWith('?activity=summary')));
 calls[0].resolve(snapshot(2));await flush();assert.equal(calls.length,4);
 calls[1].resolve(snapshot(3));calls[2].resolve(snapshot(4));calls[3].resolve(snapshot(5));await flush();
 assert.equal(cache.get(id(2)).response.text,'Reply 2');
 time=8000;cache.sync(nodes);cache.poll();await flush();assert.equal(calls.length,4);
 cache.sync([child(2,'running','version2'),...nodes.slice(1)]);cache.poll();await flush();assert.equal(calls.length,5);
 calls[4].resolve(snapshot(2,{status:'running',active:true}));await flush();cache.poll();await flush();assert.equal(calls.length,5);
 time+=4000;cache.poll();await flush();assert.equal(calls.length,6);
 calls[5].resolve(snapshot(2));await flush();cache.dispose();
});

test('response cache rejects stale results after hiding, membership changes, and disposal',async()=>{
 let visible=true,time=0,changes=0;const calls=[];
 const cache=swarm.responseCache({now:()=>time,active:()=>visible,onChange:()=>changes++,api:()=>{const pending=deferred();calls.push(pending);return pending.promise;}});
 cache.sync([child(2)]);cache.poll();await flush();assert.equal(calls.length,1);
 visible=false;cache.pause();calls[0].resolve(snapshot(2));await flush();assert.equal(changes,0);assert.equal(cache.get(id(2)).response,null);
 time=10000;cache.poll();await flush();assert.equal(calls.length,1);
 visible=true;cache.poll();await flush();assert.equal(calls.length,2);
 cache.sync([child(2,'running','version2')]);calls[1].resolve(snapshot(2));await flush();assert.equal(changes,0);assert.equal(calls.length,3);
 cache.sync([]);calls[2].resolve(snapshot(2));await flush();assert.equal(cache.get(id(2)),null);assert.equal(changes,0);
 cache.sync([child(3)]);cache.poll();await flush();assert.equal(calls.length,4);
 cache.dispose();calls[3].resolve(snapshot(3));await flush();cache.poll();assert.equal(changes,0);assert.equal(cache.get(id(3)),null);assert.equal(calls.length,4);
});

test('authorization loss clears every cached reply and fences concurrent results; deleted agents clear their reply',async()=>{
 for(const status of [401,403]){
  let time=0;const calls=[];
  const cache=swarm.responseCache({now:()=>time,api:()=>{const pending=deferred();calls.push(pending);return pending.promise;}});
  cache.sync([child(2),child(3)]);cache.poll();await flush();
  calls[0].resolve(snapshot(2));await flush();assert.equal(cache.get(id(2)).response.text,'Reply 2');
  calls[1].reject({status});await flush();assert.equal(cache.get(id(2)).response,null);assert.equal(cache.get(id(2)).error,'unavailable');
  time=20000;cache.poll();await flush();assert.equal(calls.length,2);cache.dispose();
 }
 let time=0,count=0;
 const cache=swarm.responseCache({now:()=>time,api:async()=>{if(count++===0)return snapshot(2,{status:'running',active:true});throw {status:404};}});
 cache.sync([child(2)]);cache.poll();await flush();assert.ok(cache.get(id(2)).response);
 time=4000;cache.poll();await flush();assert.equal(cache.get(id(2)).response,null);assert.equal(cache.get(id(2)).error,'unavailable');
 time=8000;cache.poll();await flush();assert.equal(count,2);cache.dispose();
});

test('a transient snapshot failure can be retried without replacing it with made-up text',async()=>{
 let calls=0;
 const cache=swarm.responseCache({api:async()=>{if(calls++===0)throw {status:503};return snapshot(2);}});
 cache.sync([child(2)]);cache.poll();await flush();assert.equal(cache.get(id(2)).response,null);assert.equal(cache.get(id(2)).error,'retry');
 cache.retry(id(2));await flush();assert.equal(calls,2);assert.equal(cache.get(id(2)).error,'');assert.equal(cache.get(id(2)).response.text,'Reply 2');cache.dispose();
});

test('shared responses retain the six most recent actual agent replies in chronological order with harness identity',()=>{
 const nodes=Array.from({length:10},(_,i)=>({id:id(i+1),label:`Agent ${i+1}`,harness:'codex'}));
 const items=swarm.sharedResponses(nodes,node=>Number.parseInt(node.id,16)===10?null:{id:node.id,text:`Actual ${node.label} <reply>`,kind:'reply',createdAt:`2026-10-10T12:00:0${Number.parseInt(node.id,16)}Z`});
 assert.deepEqual(items.map(item=>item.node.label),['Agent 4','Agent 5','Agent 6','Agent 7','Agent 8','Agent 9']);
 const html=swarm.busHTML(items,()=> 'Codex');assert.match(html,/Actual Agent 9 &lt;reply&gt;/);assert.match(html,/Codex/);assert.doesNotMatch(html,/Agent 10|<reply>/);
 assert.match(swarm.busHTML([]),/Replies will appear/);
 const graph=swarm.layout(nodes.concat({id:id(11),status:'running'}),10,true);
 assert.equal(graph.length,11);assert.equal(new Set(graph.map(node=>`${node.x}:${node.y}`)).size,11);
});


test('response cache extracts bounded public prose without retaining a live dependency on the full snapshot',async()=>{
 const payload=snapshot(2,{messages:[{id:2,role:'assistant',content:'A'.repeat(12000),status:'completed'}],events:[{kind:'tool',message:'Tool completed',data:{output:'Private tool body'}}]});
 const {proxy,revoke}=Proxy.revocable(payload,{});
 const cache=swarm.responseCache({api:async()=>proxy});
 cache.sync([child(2)]);cache.poll();await flush();revoke();
 const state=cache.get(id(2));assert.equal(state.response.text.length,2000);assert.equal(state.loading,false);
 assert.equal(state.response.kind,'reply');assert.doesNotMatch(JSON.stringify(state),/Private tool body|events|messages/);
 cache.dispose();
});
