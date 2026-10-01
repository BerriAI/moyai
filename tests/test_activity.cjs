const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const MoyaiQueue=require('../app/static/message-queue.js');
const {groups,html,duration,sync,tick}=require('../app/static/activity.js');
const stamp=n=>new Date(Date.UTC(2026,8,30,12,0,n)).toISOString();
const event=(id,kind,message,data={})=>({id,kind,message,data,created_at:stamp(id)});
function run(){return {id:'chat',status:'running',active_message_id:1,messages:[{id:1,role:'user',status:'running'},{id:2,role:'user',status:'queued'}],events:[event(1,'chat','Response started',{message_id:1})]};}
const tool=(id,call,phase,extra={})=>event(id,'tool','Run command',{turn_id:1,activity_version:1,call_id:call,phase,category:'command',command:'pytest -q',...extra});

test('queued follow-ups never capture work from the current response; restored turns retain history',()=>{
  const data=run();data.events.push(event(2,'chat','Message queued',{message_id:2}),tool(3,'a','started'),tool(4,'a','completed'),event(5,'chat','Response saved',{message_id:1}),event(6,'chat','Response started',{message_id:2}),event(7,'message','Now inspecting the follow-up',{turn_id:2}));
  data.active_message_id=2;data.messages[0].status='completed';data.messages[1].status='running';
  const turns=groups(data);assert.equal(turns.get('1').rows.length,1);assert.equal(turns.get('1').live,false);assert.equal(turns.get('2').rows[0].message,'Now inspecting the follow-up');assert.equal(turns.get('2').live,true);
  assert.match(html(turns.get('1')),/Work finished/);assert.doesNotMatch(html(turns.get('1')),/data-work-timer/);
});

test('dispatching a follow-up cannot reopen the previous completed work or start a fake timer',()=>{
  const data=run();data.status='queued';data.messages[0].status='completed';
  data.events.push(tool(2,'a','completed'),event(3,'chat','Response saved',{message_id:1}));
  let turns=groups(data);
  assert.equal(turns.get('1').status,'completed');assert.equal(turns.get('1').live,false);
  assert.match(html(turns.get('1')),/Work finished/);assert.doesNotMatch(html(turns.get('1')),/Waiting to start|data-work-timer/);
  assert.equal(html(turns.get('2')),'');assert.equal(turns.get('2').live,false);
  // The persisted terminal message also wins if the stream has not delivered its save event.
  data.events.pop();turns=groups(data);assert.equal(turns.get('1').status,'completed');
  // Conversely, a received save event wins over an older message-list fetch.
  data.messages[0].status='running';data.events.push(event(3,'chat','Response saved',{message_id:1}));
  turns=groups(data);assert.equal(turns.get('1').status,'completed');assert.equal(turns.get('1').live,false);
});

test('parallel tools and journal replay pair once, even after rotation or reconnect',()=>{
  const data=run();data.events.push(tool(2,'segment1:a','started'),tool(3,'segment1:b','started'),tool(4,'segment1:b','completed'),tool(5,'segment1:a','error',{exit_code:1}),tool(6,'segment1:b','started'),tool(7,'segment2:a','started'),event(8,'message','Update',{activity_id:'same'}),event(9,'message','Update',{activity_id:'same'}));
  const turn=groups(data).get('1');assert.equal(turn.count,3);assert.deepEqual(turn.rows.filter(row=>row.kind==='tool').map(row=>row.state),['error','completed','running']);
  assert.equal(turn.rows.filter(row=>row.kind==='message').length,1);assert.match(html(turn),/Exit code 1/);
});

test('interruption does not invent tool completion; disconnected and waiting work do not pulse',()=>{
  const data=run();data.events.push(tool(2,'a','started'));data.status='failed';
  let turn=groups(data).get('1');assert.equal(turn.rows[0].state,'unconfirmed');assert.equal(turn.live,false);assert.match(html(turn),/No completion received/);
  data.status='waiting_credential';turn=groups(data).get('1');assert.equal(turn.rows[0].state,'paused');assert.equal(turn.pulse,false);assert.match(html(turn),/Waiting for a key/);
  data.status='running';data.activity_disconnected=true;turn=groups(data).get('1');assert.equal(turn.rows[0].state,'disconnected');assert.equal(turn.pulse,false);assert.match(html(turn),/reconnecting/);
});

test('legacy events use claimed turn markers, with no raw argument or result dumps',()=>{
  const data=run();data.events.push(event(2,'tool','Using read_file',{detail:{content:'private-marker'}}),event(3,'status','Saving workspace'));
  const markup=html(groups(data).get('1'));assert.match(markup,/Using read_file/);assert.match(markup,/Saving workspace/);assert.doesNotMatch(markup,/private-marker/);
});

test('tool details and commentary are escaped; old updates remain expandable',()=>{
  const data=run();for(let i=2;i<14;i++)data.events.push(tool(i,String(i),'completed',{command:'echo <script>alert(1)</script>'}));data.events.push(event(14,'message','<img onerror=alert(1)>',{turn_id:1}));
  const markup=html(groups(data).get('1'));assert.match(markup,/Show 6 earlier updates/);assert.doesNotMatch(markup,/<script>|<img/);assert.match(markup,/&lt;script&gt;/);
});

test('timers update only text, while feed sync skips unchanged history and preserves reading position',()=>{
  assert.equal(duration(0,65000),'1m 5s');assert.equal(duration(0,3601000),'1h 0m');assert.equal(duration(100,50),'0s');
  let writes=0;const slot={dataset:{workSlot:'1'},querySelectorAll:()=>[],contains:()=>false,set innerHTML(value){writes++;this.html=value;}};
  const container={scrollHeight:1000,scrollTop:100,clientHeight:400,querySelectorAll:()=>[slot]};const data=run();data.events.push(tool(2,'a','started'));
  sync(container,data);sync(container,data);assert.equal(writes,1);assert.equal(container.scrollTop,100);
  const timer={dataset:{workTimer:Date.now()-3000},textContent:''};tick({querySelectorAll:()=>[timer]});assert.equal(timer.textContent,'3s');assert.equal(writes,1);
});

test('expanded commands survive incoming events and keep keyboard focus',()=>{
  let focused=false;
  const nodes=()=>[{dataset:{workKey:'turn:1'},open:true,querySelector:()=>({addEventListener:()=>{}})},
    {dataset:{workKey:'1:a'},open:true,querySelector:()=>({addEventListener:()=>{},focus:()=>{focused=true;}})}];
  let current=nodes();
  const slot={dataset:{workSlot:'1',workLive:'true'},contains:()=>true,querySelectorAll:()=>current,set innerHTML(value){current=nodes().map(node=>({...node,open:false}));}};
  const container={scrollHeight:1000,scrollTop:50,clientHeight:400,ownerDocument:{activeElement:{closest:()=>current[1]}},querySelectorAll:()=>[slot]};
  const data=run();data.events.push(tool(2,'a','started'));
  sync(container,data);assert.equal(current[1].open,true);assert.equal(focused,true);assert.equal(container.scrollTop,50);
  data.events.push(tool(3,'a','completed'));sync(container,data);assert.equal(current[1].open,true);
});

test('chat rendering mounts inline work and a stale fetch cannot erase streamed events or drafts',()=>{
  const script=readFileSync('app/static/app.js','utf8');
  const nodes=new Map();const renders=[];
  function node(selector){if(!nodes.has(selector))nodes.set(selector,{dataset:{},scrollHeight:800,scrollTop:400,clientHeight:400,querySelectorAll:()=>[],value:'draft kept',innerHTML:''});return nodes.get(selector);}
  const data=run();data.mode='modal';data.messages[0].content='First request';data.messages[1].content='Next request';
  const state={selected:'chat',sending:new Set(),userId:'user',drafts:{chat:'draft kept'}};
  const context={state,$:node,MoyaiQueue,MoyaiActivity:{sync:(box,run)=>renders.push(run)},esc:value=>String(value??''),messageAttachments:()=>'',renderMarkdown:value=>value,modelName:()=>'',updateChatStatus:()=>{},renderCredentialRequests:()=>{},renderApprovals:()=>{},renderSlackContext:()=>{},renderAgentDetails:()=>{}};
  vm.createContext(context);vm.runInContext(script.slice(script.indexOf('function updateChat(run'),script.indexOf('async function copyText')),context);
  context.updateChat(structuredClone(data),true);
  assert.match(node('#conversation').innerHTML,/data-work-slot="1"/);
  assert.doesNotMatch(node('#conversation').innerHTML,/Next request/);
  context.renderLiveWork(tool(2,'a','started'),false);
  context.updateChat(structuredClone(data));
  assert.equal(state.chatRun.events.length,2);assert.equal(state.drafts.chat,'draft kept');assert.equal(node('#followup').value,'draft kept');assert.equal(renders.at(-1).events[1].data.call_id,'a');
  data.status='queued';data.messages[0].status='completed';
  context.updateChat(structuredClone(data));
  const markup=node('#conversation').innerHTML;
  assert.match(markup,/Next request/);assert.doesNotMatch(markup,/>queued</);
  assert.equal((markup.match(/Next request/g)||[]).length,1);
  data.active_message_id=2;data.messages[1].status='running';context.updateChat(structuredClone(data));
  assert.equal((node('#conversation').innerHTML.match(/Next request/g)||[]).length,1);
});

test('startup recovery shows a waiting state without claiming active tool work',()=>{
  const data=run();data.status='reconnecting';data.events.push(event(2,'status','Workspace will reconnect automatically',{phase:'reconnecting'}));
  const turn=groups(data).get('1');assert.equal(turn.live,true);assert.equal(turn.pulse,false);assert.equal(turn.count,0);
  assert.match(html(turn),/Reconnecting to workspace/);assert.doesNotMatch(html(turn),/is-live/);
});

test('steering inputs share the original work timeline and do not invent another turn',()=>{
  const data=run();data.messages[1]={id:2,role:'user',status:'injected',steering_parent_id:1,content:'Also check caching'};
  data.events.push(event(2,'status','Your message is guiding the current task.',{turn_id:1,message_id:2,phase:'steering'}),tool(3,'a','started'));
  const turns=groups(data);assert.equal(turns.size,1);assert.equal(turns.get('1').live,true);assert.equal(turns.get('1').rows.length,2);
  const script=readFileSync('app/static/app.js','utf8');
  const nodes=new Map();function node(selector){if(!nodes.has(selector))nodes.set(selector,{dataset:{},scrollHeight:800,scrollTop:400,clientHeight:400,querySelectorAll:()=>[],innerHTML:''});return nodes.get(selector);}
  data.messages[0].content='Original objective';
  const context={state:{selected:'chat',sending:new Set(),userId:'user'},$:node,MoyaiQueue,MoyaiActivity:{sync:()=>{}},esc:value=>String(value??''),messageAttachments:()=>'',renderMarkdown:value=>value,modelName:()=>'',updateChatStatus:()=>{},renderCredentialRequests:()=>{},renderApprovals:()=>{},renderSlackContext:()=>{},renderAgentDetails:()=>{}};
  vm.createContext(context);vm.runInContext(script.slice(script.indexOf('function updateChat(run'),script.indexOf('async function copyText')),context);
  context.updateChat(data,true);
  const markup=node('#conversation').innerHTML;
  assert.equal((markup.match(/data-work-slot=/g)||[]).length,1);
  assert.match(markup,/data-work-slot="1"/);assert.doesNotMatch(markup,/data-work-slot="2"/);
  assert(markup.indexOf('Also check caching')<markup.indexOf('data-work-slot="1"'));
  assert.match(markup,/>Steering</);
});

test('backgrounded commands do not claim a finished process',()=>{
  const run={status:'running',active_message_id:1,messages:[{id:1,role:'user',status:'running'}],events:[
    {id:1,kind:'chat',message:'Response started',data:{message_id:1},created_at:'2026-10-01T10:00:00Z'},
    {id:2,kind:'tool',message:'Run command',data:{activity_version:1,call_id:'cmd',phase:'backgrounded',category:'command',command:'long-test'},created_at:'2026-10-01T10:00:01Z'}
  ]};
  const turn=groups(run).get('1');
  assert.equal(turn.rows[0].state,'backgrounded');
  assert.match(html(turn),/Moved to background/);
  assert.doesNotMatch(html(turn),/>Finished</);
});
