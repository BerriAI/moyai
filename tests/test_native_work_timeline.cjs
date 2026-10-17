const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const compile = name => ts.transpileModule(fs.readFileSync(path.join(__dirname, '../native/web', name), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const url = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const liveUrl = url(compile('live-transcript.ts'));
const loaded = import(url(compile('work-timeline.ts').replace('from "./live-transcript"', `from "${liveUrl}"`)));
const liveModule = import(liveUrl);
const at = second => `2026-10-17T10:00:${String(second).padStart(2, '0')}Z`;
const root = {id:'root',actor_id:'viewer',status:'running',connections:[],created_at:at(0),usage:{}};
const steering = {id:'steer',actor_id:'viewer',status:'accepted',parent_turn:'root',mode:'asap',connections:[],created_at:at(20),accepted_at:at(20),usage:{}};
const input = (id,turn,time) => ({id,turn_id:turn,role:'user',author_id:'viewer',content:'Input',created_at:time});
const progress = (id,text,time) => ({id,text,phase:'commentary',created_at:time});

test('steering preserves the earlier segment and only the answered segment collapses',async()=>{
  const {workSegments,workItems} = await loaded;
  const messages=[input(1,'root',at(0)),input(2,'steer',at(20))];
  let segments=workSegments(messages,[root,steering]);
  assert.equal(segments.get(1).earlier,true);
  assert.equal(segments.get(1).hasFinalAnswer,false);
  assert.equal(segments.get(2).turn.id,'root');
  assert.equal(segments.get(2).hasFinalAnswer,false);
  const saved={messages:[progress('one','Before steering',at(5)),progress('two','After steering',at(25))]};
  assert.deepEqual(workItems(segments.get(1),saved).map(x=>x.message.text),['Before steering']);
  assert.deepEqual(workItems(segments.get(2),saved).map(x=>x.message.text),['After steering']);
  messages.push({id:3,turn_id:'root',role:'assistant',author_id:null,content:'The final answer',created_at:at(40)});
  segments=workSegments(messages,[{...root,status:'completed'},steering]);
  assert.equal(segments.get(1).hasFinalAnswer,false,'a later answer must not erase the pre-steer activity');
  assert.equal(segments.get(2).hasFinalAnswer,true);
});

test('stopping or completing without an answer does not create a final-answer transition',async()=>{
  const {workSegments} = await loaded;
  for(const status of ['stopped','failed','completed','resumed']){
    const segment=workSegments([input(1,'root',at(0))],[{...root,status}]).get(1);
    assert.equal(segment.hasFinalAnswer,false);
  }
});

test('promoted steering uses acceptance time and cannot inherit another requester’s progress',async()=>{
  const {workSegments,rootTurn} = await loaded;
  const promoted={...steering,created_at:at(1),accepted_at:at(30)};
  const segments=workSegments([input(1,'root',at(0)),input(2,'steer',at(1))],[root,promoted]);
  assert.equal(segments.get(1).before,Date.parse(at(30)));
  assert.equal(rootTurn({...steering,actor_id:'other'},[root,steering]),undefined);
  assert.equal(rootTurn({...root,parent_turn:'root'},[{...root,parent_turn:'root'}]),undefined);
});

test('saved and streamed updates reconcile without duplication or losing repeated messages',async()=>{
  const {mergeProgress} = await loaded;
  const first=progress('commentary:root:1:0','Checking.',at(5));
  const second=progress('commentary:root:2:0','Checking.',at(15));
  assert.equal(mergeProgress([first,second],[first,second]).length,2);
  const legacy={...first,id:'old-provider-id'};
  assert.equal(mergeProgress([legacy],[first,second]).length,2);
  assert.equal(mergeProgress([first],[{...first,created_at:at(7)}])[0].created_at,at(5));
});

test('child final answers stay labeled inside their agent and never finish the parent',async()=>{
  const {workSegments,workItems} = await loaded;
  const segment=workSegments([input(1,'root',at(0))],[root]).get(1);
  const agent={id:'child',parent_id:'root',parent_name:null,name:'Kepler',avatar:0,depth:0,status:'completed',
    created_at:at(10),updated_at:at(20),messages:[{id:'child-final',text:'Child result',phase:'final_answer',created_at:at(20)}],
    tools:[],messages_limited:false,tools_limited:false};
  const items=workItems(segment,{messages:[progress('parent','Inspecting',at(5))],subagents:[agent]});
  assert.deepEqual(items.map(x=>x.kind),['message','agent']);
  assert.equal(segment.hasFinalAnswer,false);
  assert.equal(items[1].agent.messages[0].text,'Child result');
});

test('real tool lifecycle groups sit between updates and survive stream/snapshot reconciliation',async()=>{
  const {workSegments,workItems} = await loaded;
  const {emptyLiveTranscript,applyLiveEvent:apply}=await liveModule;
  const segment=workSegments([input(1,'root',at(0))],[root]).get(1);
  let live=apply(emptyLiveTranscript('root'),{type:'tool_call',id:'tool',name:'shell'},0,at(10));
  live=apply(live,{type:'tool_result',id:'tool',name:'shell'},1,at(15));
  const items=workItems(segment,{messages:[progress('before','Inspecting',at(5)),progress('after','Verified',at(20))],
    tools:[{id:'tool',name:'shell',status:'running',created_at:at(9),finished_at:null}]},live);
  assert.deepEqual(items.map(x=>x.kind),['message','tools','message']);
  assert.equal(items[1].tools.length,1);
  assert.equal(items[1].tools[0].status,'completed');
  assert.equal(items[1].tools[0].created_at,at(9));
});
