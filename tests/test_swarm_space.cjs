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
