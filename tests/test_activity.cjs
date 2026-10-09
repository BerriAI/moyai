const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
const MoyaiQueue=require('../app/static/message-queue.js');
const MoyaiActivity=require('../app/static/activity.js');
const {groups,current,timeline,updates,html,duration,syncWork,tick}=MoyaiActivity;
const stamp=n=>new Date(Date.UTC(2026,8,30,12,0,n)).toISOString();
const event=(id,kind,message,data={})=>({id,kind,message,data,created_at:stamp(id)});
function run(){return {id:'chat',status:'running',active_message_id:1,messages:[{id:1,role:'user',status:'running'},{id:2,role:'user',status:'queued'}],events:[event(1,'chat','Response started',{message_id:1})]};}
const tool=(id,call,phase,extra={})=>event(id,'tool','Run command',{turn_id:1,activity_version:1,call_id:call,phase,category:'command',command:'pytest -q',...extra});
const focus=(id,message,input=1,extra={})=>event(id,'status',message,{turn_id:1,input_id:input,activity_version:1,phase:'focus',live_status:true,activity_id:`focus-${id}`,...extra});

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

test('a completed answer stops the work timer while workspace saving stays separate',()=>{
  const data=run();data.events.push(tool(2,'a','started'),tool(3,'a','completed'),focus(4,'Checking the answer'),
    event(9,'chat','Response received',{message_id:1,response_complete:true}));
  let turn=current(data);
  assert.equal(turn.end,Date.parse(stamp(9)));assert.equal(turn.status,'saving');
  assert.equal(turn.headline,'Saving workspace');assert.equal(turn.live,false);assert.equal(turn.pulse,false);
  assert.equal(data.status,'running');assert.equal(data.messages[0].status,'running');
  assert.match(html(turn),/Worked for 8s/);assert.doesNotMatch(html(turn),/data-work-timer|is-live|✓/);
  const block=timeline(data).get('1')[0].block;
  assert.equal(block.end,turn.end);assert.equal(block.live,false);
  // Later maintenance and even delayed tool receipts cannot extend answer latency.
  data.events.push(tool(11,'a','completed'),event(12,'status','Saving workspace'));
  assert.equal(timeline(data).get('1')[0].block.end,Date.parse(stamp(9)));
  data.status='saving';assert.equal(current(data).pulse,false);
  data.events.push(event(42,'chat','Response saved',{message_id:1}));data.messages[0].status='completed';data.status='idle';
  turn=current(data);assert.equal(turn.end,Date.parse(stamp(9)));assert.equal(turn.status,'completed');
  assert.match(html(turn),/Worked for 8s/);
  assert.deepEqual(current(JSON.parse(JSON.stringify(data))),turn,'reload retains the answer receipt timestamp');
});

test('continuation, legacy and wrong-turn receipts cannot finish live work',()=>{
  for(const extra of [{message_id:1},{message_id:1,response_complete:false},{message_id:2,response_complete:true},{message_id:1,response_complete:'true'}]){
    const data=run();data.events.push(event(9,'chat','Response received',extra));
    const turn=current(data);assert.equal(turn.live,true);assert.equal(turn.pulse,true);assert.equal(turn.end,0);
    assert.equal(turn.status,'running');
  }
});

test('receipt-only answers keep a frozen work block and later failures remain visible',()=>{
  const data=run();data.events.push(event(9,'chat','Response received',{message_id:1,response_complete:true}));
  let block=timeline(data).get('1')[0].block;
  assert.equal(block.end,Date.parse(stamp(9)));assert.match(html(block),/Worked for 8s/);
  data.status='failed';data.checkpoint_error='Workspace save failed';
  assert.equal(current(data).headline,'Response failed');assert.equal(current(data).pulse,false);
  assert.doesNotMatch(html(current(data)),/Worked for|✓/);
  // A save event ahead of a fresh message list must not mask the failure.
  data.events.push(event(42,'chat','Response saved',{message_id:1}));
  assert.equal(current(data).status,'failed');
  data.messages[0].status='save_failed';block=timeline(data).get('1')[0].block;
  assert.equal(block.headline,'Workspace save failed');assert.equal(block.end,Date.parse(stamp(9)));
  assert.match(html(block),/Workspace save failed/);assert.doesNotMatch(html(block),/data-work-timer|✓/);
});

test('parallel tools and journal replay pair once, even after rotation or reconnect',()=>{
  const data=run();data.events.push(tool(2,'segment1:a','started'),tool(3,'segment1:b','started'),tool(4,'segment1:b','completed'),tool(5,'segment1:a','error',{exit_code:1}),tool(6,'segment1:b','started'),tool(7,'segment2:a','started'),event(8,'message','Update',{activity_id:'same'}),event(9,'message','Update',{activity_id:'same'}));
  const turn=groups(data).get('1');assert.equal(turn.count,3);assert.deepEqual(turn.rows.filter(row=>row.kind==='tool').map(row=>row.state),['error','completed','running']);
  assert.equal(turn.rows.filter(row=>row.kind==='message').length,1);assert.match(html(turn),/Exit code 1/);
});

test('interruption does not invent tool completion; disconnected and waiting work do not pulse',()=>{
  const data=run();data.events.push(tool(2,'a','started'));data.status='failed';
  let turn=groups(data).get('1');assert.equal(turn.rows[0].state,'unconfirmed');assert.equal(turn.live,false);assert.match(html(turn),/No completion received/);
  data.status='waiting_credential';turn=groups(data).get('1');assert.equal(turn.rows[0].state,'paused');assert.equal(turn.pulse,false);assert.match(html(turn),/Waiting for access/);
  data.status='running';data.activity_disconnected=true;turn=groups(data).get('1');assert.equal(turn.rows[0].state,'disconnected');assert.equal(turn.pulse,false);assert.match(html(turn),/reconnecting/);
});

test('legacy events use claimed turn markers, with no raw argument or result dumps',()=>{
  const data=run();data.events.push(event(2,'tool','Using read_file',{detail:{content:'private-marker'}}),event(3,'status','Saving workspace'));
  const markup=html(groups(data).get('1'));assert.match(markup,/Using read_file/);assert.match(markup,/Saving workspace/);assert.doesNotMatch(markup,/private-marker/);
});

test('tool details are escaped; public commentary is excluded from expandable work history',()=>{
  const data=run();for(let i=2;i<14;i++)data.events.push(tool(i,String(i),'completed',{command:'echo <script>alert(1)</script>'}));data.events.push(event(14,'message','<img onerror=alert(1)>',{turn_id:1}));
  const markup=html(groups(data).get('1'));assert.match(markup,/Show 5 earlier updates/);assert.doesNotMatch(markup,/<script>|<img/);assert.match(markup,/&lt;script&gt;/);
});

test('every tool expands with escaped payloads, image references or an explicit missing-details message',()=>{
  const data=run();
  data.events.push(tool(2,'repo','completed',{category:'tool',command:null,input:'{"id":123}',output:'<script>bad()</script>'}),
    tool(3,'image','completed',{category:'image',command:null,path:'/workspace/image.png',image_path:'/workspace/moyai-tool-images/snapshot.png',image_preview:'data:image/jpeg;base64,YWJj'}),
    tool(4,'private','completed',{category:'tool',command:null,details_notice:'Credential payloads are private.'}),
    event(5,'tool','Legacy tool'));
  const markup=html(groups(data).get('1'));
  assert.equal((markup.match(/<details data-work-key=/g)||[]).length,4);
  assert.match(markup,/&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.match(markup,/data-file-ref="\/workspace\/moyai-tool-images\/snapshot.png"/);
  assert.match(markup,/<img src="data:image\/jpeg;base64,YWJj"/);
  assert.match(markup,/Credential payloads are private/);
  assert.match(markup,/Inputs and results were not recorded/);
  assert.doesNotMatch(markup,/<script>|src="\/workspace/);
  data.events[2].data.image_preview='https://untrusted.example/tracker';
  assert.doesNotMatch(html(groups(data).get('1')),/untrusted.example/);
});

test('timers update only text and unchanged activity blocks keep their DOM',()=>{
  assert.equal(duration(0,65000),'1m 5s');assert.equal(duration(0,3601000),'1h 0m');assert.equal(duration(100,50),'0s');
  let writes=0;const slot={dataset:{workSlot:'1'},querySelectorAll:()=>[],contains:()=>false,set innerHTML(value){writes++;this.html=value;}};
  const data=run();data.events.push(tool(2,'a','started'));
  syncWork(slot,groups(data).get('1'));syncWork(slot,groups(data).get('1'));assert.equal(writes,1);
  const timer={dataset:{workTimer:Date.now()-3000},textContent:''};tick({querySelectorAll:()=>[timer]});assert.equal(timer.textContent,'3s');assert.equal(writes,1);
});

test('expanded commands survive incoming events and keep keyboard focus',()=>{
  let focused=false;
  const nodes=()=>[{dataset:{workKey:'turn:1'},open:true,querySelector:()=>({addEventListener:()=>{}})},
    {dataset:{workKey:'1:a'},open:true,querySelector:()=>({addEventListener:()=>{},focus:()=>{focused=true;}})}];
  let current=nodes();
  const slot={dataset:{workSlot:'1',workLive:'true'},contains:()=>true,querySelectorAll:()=>current,set innerHTML(value){current=nodes().map(node=>({...node,open:false}));}};
  const container={scrollHeight:1000,scrollTop:50,clientHeight:400,ownerDocument:{activeElement:{closest:()=>current[1]}},querySelectorAll:selector=>selector==='[data-activity-slot]'?[slot]:[]};
  const data=run();data.events.push(tool(2,'a','started'));
  slot.ownerDocument=container.ownerDocument;syncWork(slot,groups(data).get('1'));assert.equal(current[1].open,true);assert.equal(focused,true);assert.equal(container.scrollTop,50);
  data.events.push(tool(3,'a','completed'));syncWork(slot,groups(data).get('1'));assert.equal(current[1].open,true);
});

test('chat rendering mounts inline work and a stale fetch cannot erase streamed events or drafts',()=>{
  const script=readFileSync('app/static/app.js','utf8');
  const nodes=new Map();const renders=[];
  function node(selector){if(!nodes.has(selector))nodes.set(selector,{dataset:{},scrollHeight:800,scrollTop:400,clientHeight:400,querySelectorAll:()=>[],querySelector:node,setAttribute(name,value){this[name]=value;},value:'draft kept',innerHTML:''});return nodes.get(selector);}
  const data=run();data.mode='modal';data.messages[0].content='First request';data.messages[1].content='Next request';
  const state={selected:'chat',sending:new Set(),userId:'user',drafts:{chat:'draft kept'}};
  const context={savedFiles:{sync(){},decorate(){}},state,$:node,MoyaiQueue,MoyaiActivity:{sync:(box,run)=>renders.push(run)},esc:value=>String(value??''),messageAttachments:()=>'',renderMarkdown:value=>value,copyText:()=>{},modelName:()=>'',updateChatStatus:()=>{},renderChatWorking:()=>{},renderCredentialRequests:()=>{},renderApprovals:()=>{},renderPrWriteAccess:()=>{},renderSlackContext:()=>{},renderAgentDetails:()=>{}};
  vm.createContext(context);vm.runInContext(script.slice(script.indexOf('function syncChatComposer('),script.indexOf('function updateChatStatus('))+
    script.slice(script.indexOf('function updateChat(run'),script.indexOf('async function copyText')),context);
  context.updateChat(structuredClone(data),true);
  assert.match(node('#conversation').innerHTML,/data-activity-slot="1"/);
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
  const nodes=new Map();function node(selector){if(!nodes.has(selector))nodes.set(selector,{dataset:{},scrollHeight:800,scrollTop:400,clientHeight:400,querySelectorAll:()=>[],querySelector:node,setAttribute(name,value){this[name]=value;},innerHTML:''});return nodes.get(selector);}
  data.messages[0].content='Original objective';
  const context={savedFiles:{sync(){},decorate(){}},state:{selected:'chat',sending:new Set(),userId:'user'},$:node,MoyaiQueue,MoyaiActivity:{sync:()=>{}},esc:value=>String(value??''),messageAttachments:()=>'',renderMarkdown:value=>value,copyText:()=>{},modelName:()=>'',updateChatStatus:()=>{},renderCredentialRequests:()=>{},renderApprovals:()=>{},renderPrWriteAccess:()=>{},renderSlackContext:()=>{},renderAgentDetails:()=>{}};
  vm.createContext(context);vm.runInContext(script.slice(script.indexOf('function syncChatComposer('),script.indexOf('function updateChatStatus('))+
    script.slice(script.indexOf('function updateChat(run'),script.indexOf('async function copyText')),context);
  context.updateChat(data,true);
  const markup=node('#conversation').innerHTML;
  assert.equal((markup.match(/data-activity-slot=/g)||[]).length,2);
  assert.match(markup,/data-activity-slot="1"/);assert.match(markup,/data-activity-slot="2"/);
  assert(markup.indexOf('data-activity-slot="1"')<markup.indexOf('Also check caching'));
  assert.match(markup,/>Steering</);
  assert(markup.indexOf('Also check caching')<markup.indexOf('data-activity-slot="2"'));
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

test('tools and public replies stay on each side of multiple injected messages, including late completion',()=>{
  const data=run();data.messages[1]={id:2,role:'user',status:'injected',steering_parent_id:1};
  data.messages.push({id:3,role:'user',status:'injected',steering_parent_id:1});
  data.events.push(tool(2,'before','started'),event(3,'message','Initial finding'),
    tool(4,'also-before','completed'),event(5,'status','Delivered',{turn_id:1,message_id:2,phase:'steering'}),
    event(6,'message','Answer to your question'),tool(7,'after','completed'),
    tool(8,'before','completed'),event(9,'message','Verified the change'),
    event(10,'status','Delivered',{turn_id:1,message_id:3,phase:'steering'}),tool(11,'last','started'));
  const entries=timeline(data);
  const order=id=>entries.get(id).flatMap(item=>item.type==='update'?item.content:item.block.rows.map(row=>row.id));
  assert.deepEqual(order('1'),['before','Initial finding','also-before']);
  assert.deepEqual(order('2'),['Answer to your question','after','Verified the change']);
  assert.deepEqual(order('3'),['last']);
  assert.equal(entries.get('1')[0].block.rows[0].state,'completed','a completion updates the original row without moving it');
  assert.equal(entries.get('1')[0].block.live,false);
  assert.equal(entries.get('3')[0].block.live,true);
  assert.deepEqual(timeline(JSON.parse(JSON.stringify(data))),entries,'reload reconstructs the same order');
  data.events.push(event(12,'chat','Response saved',{message_id:1}));data.messages[0].status='completed';
  assert.equal(timeline(data).get('3')[0].block.live,false);
  assert.equal(timeline(data).get('3')[0].block.rows[0].state,'unconfirmed');
});

test('emission input tags keep delayed activity above the injection, independently of journal arrival',()=>{
  const data=run();data.messages[1]={id:2,role:'user',status:'injected',steering_parent_id:1};
  data.events.push(event(2,'status','Delivered',{turn_id:1,message_id:2,phase:'steering'}),
    tool(3,'old-buffered-tool','started',{input_id:1}),
    event(4,'message','Before the correction',{turn_id:1,input_id:1}),
    event(5,'message','After the correction',{turn_id:1,input_id:2}),
    tool(6,'new-tool','completed',{input_id:2}),
    tool(7,'old-buffered-tool','completed',{input_id:2}));
  const entries=timeline(data);
  assert.equal(entries.get('1')[0].block.rows[0].id,'old-buffered-tool');
  assert.equal(entries.get('1')[1].content,'Before the correction');
  assert.equal(entries.get('2')[0].content,'After the correction');
  assert.deepEqual(entries.get('2')[1].block.rows.map(row=>row.id),['new-tool']);
  assert.equal(entries.get('1')[0].block.rows[0].state,'completed');
});

test('queueing an input does not split the current work; delivery while no new tool exists still shows live work',()=>{
  const data=run();data.events.push(tool(2,'original','completed'),event(3,'chat','Message queued',{message_id:2}));
  assert.equal(timeline(data).get('2')?.length||0,0);
  data.messages[1]={id:2,role:'user',status:'injected',steering_parent_id:1,started_at:stamp(4)};
  data.events.push(event(4,'status','Delivered',{turn_id:1,message_id:2,phase:'steering'}));
  assert.equal(timeline(data).get('1')[0].block.count,1);
  assert.equal(timeline(data).get('1')[0].block.live,false);
  assert.equal(timeline(data).get('2')[0].block.count,0);
  assert.equal(timeline(data).get('2')[0].block.live,true);
});

test('approved focus changes one collapsed headline without creating rows, cards, or work blocks',()=>{
  const data=run();data.events.push(tool(2,'inspect','started'));
  const block=timeline(data).get('1')[0].id;
  for(const [id,message] of [[3,'Reading the request'],[4,'Auditing UI and schema changes'],[5,'Checking the updated behavior']]){
    data.events.push(focus(id,message));
    const turn=current(data),items=timeline(data).get('1');
    assert.equal(turn.headline,message);assert.equal(turn.summary,message);
    assert.equal(turn.count,1);assert.equal(turn.rows.length,1);assert.equal(updates(data).size,0);
    assert.equal(items.length,1);assert.equal(items[0].id,block);
    assert.doesNotMatch(html(turn),/<details class="turn-work[^>]*\bopen\b/);
  }
  data.events.push(focus(6,'Reading the request',1,{activity_id:'focus-3'}),focus(7,'Unapproved text',1,{live_status:false}),
    focus(8,'Wrong turn',9,{turn_id:9}),focus(9,'Wrong input',2));
  assert.equal(current(data).headline,'Checking the updated behavior');
  assert.deepEqual(timeline(JSON.parse(JSON.stringify(data))),timeline(data),'reload restores the latest approved headline');
  data.active_message_id=2;data.messages[1].status='running';
  assert.equal(groups(data).get('1').headline,'Reviewing the task','a stale running message cannot keep a previous turn focus live');
  const bare=run();bare.events.push(tool(2,'a','started'),event(3,'message','Long public commentary'),event(4,'status','Raw process text',{phase:'processing'}));
  assert.equal(current(bare).headline,'Reviewing the task');assert.equal(current(bare).summary,'Reviewing the task');
});

test('focus scope follows offered inputs and receipt order, including an older input delivered last',()=>{
  const data=run();data.events.push(focus(2,'Inspecting the original request'));
  data.messages.push({id:3,role:'user',status:'injected',steering_parent_id:1});
  assert.equal(current(data).headline,'Inspecting the original request','queued follow-ups do not invalidate current focus');
  data.events.push(event(3,'status','Delivered',{turn_id:1,message_id:3,phase:'steering'}));
  assert.equal(current(data).headline,'Reviewing the task');
  data.events.push(focus(4,'Checking the newer input',3));assert.equal(current(data).headline,'Checking the newer input');
  Object.assign(data.messages[1],{queue_locked:1,steering_parent_id:1});
  assert.equal(current(data).headline,'Reviewing the task','an offered input invalidates prior focus before acknowledgement');
  data.events.push(focus(5,'Checking the older input',2));assert.equal(current(data).headline,'Checking the older input');
  data.events.push(event(6,'status','Delivered',{turn_id:1,message_id:2,phase:'steering'}),focus(7,'Delayed older scope',3));
  assert.equal(current(data).headline,'Checking the older input');
  data.events.push(event(8,'status','Delivered',{turn_id:1,message_id:4,phase:'steering'}));
  assert.equal(current(data).headline,'Reviewing the task','a stale locked message snapshot cannot undo a newer receipt');
  data.events.push(focus(9,'Reading the last correction',4));
  assert.equal(current(data).headline,'Reading the last correction','receipts work before the new message list is fetched');
  assert.equal(current(JSON.parse(JSON.stringify(data))).headline,'Reading the last correction');
  data.events.push(event(10,'chat','Response saved',{message_id:1}),event(11,'chat','Response started',{message_id:5}));
  data.messages[0].status='completed';data.messages.push({id:5,role:'user',status:'running'});data.active_message_id=5;
  assert.equal(current(data).headline,'Reviewing the task','a new turn never inherits old focus');
});

test('waits, stopping, disconnection and completion override a saved focus and stop its pulse',()=>{
  const data=run();data.events.push(focus(2,'Auditing UI and schema changes'));
  for(const [status,label] of Object.entries({awaiting_approval:'Waiting for approval',waiting_children:'Waiting for agents',waiting_credential:'Waiting for access',reconnecting:'Reconnecting to workspace',stopping:'Stopping',deleting:'Deleting session',cancelled:'Stopped',failed:'Response failed',idle:'Work finished'})){
    data.status=status;assert.equal(current(data).headline,label);assert.equal(current(data).pulse,false);
  }
  data.status='running';data.activity_disconnected=true;
  assert.equal(current(data).headline,'Connection lost · reconnecting');assert.equal(current(data).pulse,false);
  data.activity_disconnected=false;assert.equal(current(data).headline,'Auditing UI and schema changes');
  data.events.push(event(3,'chat','Response saved',{message_id:1}));
  assert.equal(current(data).headline,'Work finished');assert.equal(current(data).pulse,false);
});

test('manual expansion and collapse survive focus changes and completion with keyboard focus retained',()=>{
  let focused=false,currentNode;
  const node=()=>({dataset:{workKey:'turn:1'},open:false,querySelector(){return {addEventListener:(name,fn)=>{this.click=fn;},focus:()=>{focused=true;}};}});
  const slot={dataset:{},ownerDocument:{activeElement:null},contains:()=>!!slot.ownerDocument.activeElement,
    querySelectorAll:()=>currentNode?[currentNode]:[],set innerHTML(value){currentNode=node();}};
  const data=run();data.events.push(focus(2,'Inspecting the task'));syncWork(slot,current(data));
  assert.equal(currentNode.open,false);
  currentNode.click();currentNode.open=true;slot.ownerDocument.activeElement={closest:()=>currentNode};
  data.events.push(focus(3,'Verifying the result'));syncWork(slot,current(data));
  assert.equal(currentNode.open,true);assert.equal(focused,true);
  data.status='completed';syncWork(slot,current(data));assert.equal(currentNode.open,true);
  currentNode.click();currentNode.open=false;data.status='running';syncWork(slot,current(data));
  assert.equal(currentNode.open,false);
});

test('the composer follows live SSE focus, reconnects and lifecycle state without exposing focus history',()=>{
  const script=readFileSync('app/static/app.js','utf8'),nodes=new Map(),sources=[],refreshes=[];
  const node=selector=>{
    if(selector==='#activity-history'||selector==='#goal-status')return null;
    if(!nodes.has(selector))nodes.set(selector,{dataset:{},textContent:'',classList:{toggle(name,value){this[name]=value;}},setAttribute(name,value){this[name]=value;},querySelectorAll:()=>[],querySelector:node,insertAdjacentHTML(){}});
    return nodes.get(selector);
  };
  const data=run(),state={selected:'chat',chatRun:data,modelDrafts:{},runs:[],sending:new Set()};
  const context={state,document:{hidden:false},$:node,MoyaiActivity,MoyaiGoal:require('../app/static/goal-status.js'),terminal:new Set(['completed','failed','cancelled','interrupted','idle']),savedFiles:{decorate(){}},
    renderMarkdown:text=>text,esc:text=>text,copyText(){},modelName:()=>'',statusLabel:text=>text,renderSidebar(){},
    refreshChat:async id=>refreshes.push(id),showError:error=>{throw error;},clearTimeout(){},setTimeout(){},
    EventSource:class{constructor(){sources.push(this);this.handlers={};}addEventListener(name,handler){this.handlers[name]=handler;}close(){}}};
  vm.createContext(context);vm.runInContext(
    script.slice(script.indexOf('function sessionTitle('),script.indexOf('function modelName('))+
    script.slice(script.indexOf('function connectChatStream('),script.indexOf('function updateChat(run'))+
    script.slice(script.indexOf('function renderLiveWork('),script.indexOf('async function copyText'))+
    script.slice(script.indexOf('function eventHTML('),script.indexOf('function renderApprovals(')),context);
  context.updateChatStatus(data);assert.equal(node('#chat-working').textContent,'Reviewing the task');
  context.connectChatStream(data);const source=sources[0];
  source.onmessage({data:JSON.stringify(focus(2,'Auditing UI and schema changes'))});
  assert.equal(node('#chat-working').textContent,'Auditing UI and schema changes');assert.equal(node('#chat-working').classList.busy,true);
  source.onmessage({data:JSON.stringify(focus(3,'Verifying the updated behavior'))});
  assert.equal(node('#chat-working').textContent,'Verifying the updated behavior');assert.equal(context.eventHTML(data.events.at(-1)),'');
  source.onerror();assert.equal(node('#chat-working').textContent,'Connection lost · reconnecting');assert.equal(node('#chat-working').classList.busy,false);
  source.onopen();assert.equal(node('#chat-working').textContent,'Verifying the updated behavior');
  source.onmessage({data:JSON.stringify(focus(4,'Handling the correction',2))});
  assert.equal(refreshes.length,2,'an offered input ahead of the local snapshot fetches authoritative scope');
  source.handlers['run-status']({data:JSON.stringify({status:'waiting_credential'})});
  assert.match(node('#chat-working').textContent,/Waiting for access/);assert.equal(node('#chat-working').classList.busy,false);
  source.handlers['run-status']({data:JSON.stringify({status:'running'})});
  source.onmessage({data:JSON.stringify(event(9,'chat','Response received',{message_id:1,response_complete:true}))});
  assert.equal(node('#chat-working').textContent,'Saving workspace');assert.equal(node('#chat-working').classList.busy,false);
  source.handlers['run-status']({data:JSON.stringify({status:'idle'})});assert.equal(node('#chat-working').textContent,'');
  const controls=['#stop-response','#message-form .send-button','#message-form [data-send-now]','#chat-model'].map(node);
  node('#message-form').querySelectorAll=()=>controls;
  node('#followup').value='Unsent reply';let locked=false;state.attachments={lock:value=>locked=value};
  source.handlers['run-status']({data:JSON.stringify({status:'deleting'})});
  assert.equal(node('#chat-working').textContent,'Deleting session…');assert.equal(controls.every(control=>control.disabled),true);
  assert.equal(node('#followup').contentEditable,'false');assert.equal(node('#followup')['aria-disabled'],'true');
  assert.equal(node('#followup').value,'Unsent reply');assert.equal(locked,true);
  assert.match(node('#queue-note').textContent,/close automatically/);
  source.handlers['run-status']({data:JSON.stringify({status:'deleting',deletion_error:'Cleanup will retry automatically.'})});
  assert.match(node('#chat-working').textContent,/Cleanup will retry automatically/);
  source.handlers['run-status']({data:JSON.stringify({status:'idle'})});
  assert.match(node('#chat-working').textContent,/Cleanup will retry automatically/);
  assert.equal(controls.every(control=>control.disabled),true);
});

test('side-chat polls replace focus while retaining expanded activity through status and transcript updates',()=>{
  const script=readFileSync('app/static/workspace-panel.js','utf8');let writes=0,syncs=0;
  const log={slots:[],scrollHeight:1000,scrollTop:50,clientHeight:400,querySelectorAll:()=>log.slots,
    set innerHTML(markup){writes++;this.slots=[...markup.matchAll(/data-activity-slot="(\d+)"/g)].map(([,id])=>({dataset:{activitySlot:id},
      details:{open:false},replaceWith(previous){log.slots[log.slots.indexOf(this)]=previous;}}));}};
  const context={current:null,signature:'',deleting:false,deletionError:'',sending:false,stopping:false,unavailable:false,form:{},input:{},model:{},log,status:{},stop:{},send:{},link:{},t:{chatId:'side-chat'},MoyaiQueue,syncTitles(){},
    MoyaiActivity:{...MoyaiActivity,sync(container,data){syncs++;container.slots[0].headline=current(data).headline;},tick(){}},
    markdown:text=>text,esc:text=>text,toast(){}};
  vm.createContext(context);vm.runInContext(script.slice(script.indexOf('function syncControls()'),script.indexOf('model.onchange='))+
    script.slice(script.indexOf('function drawChat(data)'),script.indexOf('async function poll()')),context);
  const data=run();data.messages=[{...data.messages[0],content:'Explain the UI change'}];
  context.drawChat(data);const slot=log.slots[0];slot.details.open=true;
  data.events.push(focus(2,'Auditing the side-chat changes'));context.drawChat(data);
  assert.equal(context.status.textContent,'Auditing the side-chat changes');assert.equal(slot.headline,context.status.textContent);
  assert.equal(writes,1);assert.equal(syncs,2);assert.equal(log.slots[0],slot);assert.equal(slot.details.open,true);
  data.events.push(focus(3,'Verifying the side-chat answer'));context.drawChat(data);
  assert.equal(context.status.textContent,'Verifying the side-chat answer');assert.equal(writes,1);
  data.events.push(event(9,'chat','Response received',{message_id:1,response_complete:true}));context.drawChat(data);
  assert.equal(context.status.textContent,'Saving workspace');assert.equal(slot.headline,context.status.textContent);
  assert.equal(writes,1);assert.equal(log.slots[0],slot);assert.equal(slot.details.open,true);
  data.status='reconnecting';context.drawChat(data);assert.equal(context.status.textContent,'Reconnecting…');assert.equal(log.slots[0],slot);
  data.messages.push({id:3,role:'assistant',status:'completed',content:'A useful finding'});context.drawChat(data);
  assert.equal(writes,2);assert.equal(log.slots[0],slot);assert.equal(slot.details.open,true);assert.equal(log.scrollTop,50);
  data.status='idle';context.drawChat(data);assert.equal(context.status.textContent,'');
});

test('runtime compaction notices render as quiet disclosures while ordinary prose stays intact',()=>{
  const notices=[
    ['Compacting saved context before continuing. Completed tool receipts are preserved.','Context compaction'],
    ['The agent compacted its context and is continuing. Completed tool receipts remain saved.','Context compacted'],
  ];
  for(const [content,label] of notices){
    const rendered=MoyaiActivity.updateHTML({id:'a"b',content},()=>{throw new Error('Runtime notice is not Markdown');});
    assert.match(rendered,/<details class="context-compaction"/);
    assert.ok(rendered.includes(label));
    assert.ok(rendered.includes(content));
    assert.match(rendered,/data-update-id="a&quot;b"/);
    assert.doesNotMatch(rendered,/copy-update|assistant-update|animation/);
  }
  for(const content of ['I am compacting the context.', '> '+notices[0][0], 'toString']){
    assert.match(MoyaiActivity.updateHTML({id:1,content}),/assistant-update/);
  }
});
