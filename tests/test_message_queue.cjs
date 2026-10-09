const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const queue=require('../app/static/message-queue.js');
const message={id:2,role:'user',user_id:'alice',user_name:'Alice',status:'queued',content:'Original message',model:'astra',revision:0,queue_locked:0,attachments:[]};

const active={id:1,role:'user',status:'running',content:'Current request'};

test('historical handoff notices stay out of chat while real failures remain visible',()=>{
  const run={status:'idle',messages:[{...active,status:'steered'},
    {id:3,role:'assistant',status:'steered',content:'Paused and saved'},
    {id:4,role:'assistant',status:'save_failed',content:'Real answer; saving failed'}]};
  assert.deepEqual(queue.presentation(run).transcript.map(m=>m.id),[1,4]);
});

function controller(){
  const requests=[],toasts=[],drafts=new Map(),used=[];let run={id:'run',messages:[active,{...message}]},writes=0;
  const element={ownerDocument:{},hidden:true,querySelectorAll:()=>[],querySelector:()=>null,set innerHTML(text){this.html=text;writes++;}};
  const options={element,runId:'run',user:'alice',role:'member',drafts,toast:value=>toasts.push(value),useDraft:text=>used.push(text),api:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {revision:1};},refresh:async()=>ctrl.render(run)};
  const ctrl=queue.create(options);ctrl.render(run);
  return {ctrl,requests,toasts,drafts,used,element,options,get writes(){return writes;},setRun(next){run=next;ctrl.render(next);}};
}

test('Slack reply cards show escaped names while queue edits preserve canonical input',async()=>{
  const b=controller();
  const input={...message,content:'Slack reply from U12345678:\nMake a PR',
    display_content:'Slack reply from Ryan <script>alert(1)</script>:\nMake a PR'};
  b.setRun({status:'running',messages:[active,input]});
  assert.match(b.element.html,/Slack reply from Ryan &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(b.element.html,/U12345678|<script>/);
  await b.ctrl.act(2,'edit');
  assert.equal(b.drafts.get(2).content,input.content);
  await b.ctrl.act(2,'save');
  assert.equal(b.requests[0].body.content,input.content);
});

test('resolved CC mentions remain plain text, including hostile profile names',()=>{
  const b=controller();
  b.setRun({status:'running',messages:[active,{...message,
    content:'cc: <@U87654321> can you help?',
    display_content:'cc: @Tin <img src=x onerror=alert(1)> & teammates can you help?'}]});
  assert.match(b.element.html,/cc: @Tin &lt;img src=x onerror=alert\(1\)&gt; &amp; teammates/);
  assert.doesNotMatch(b.element.html,/<img|U87654321/);
});

test('a new request is immediately in the transcript without changing its durable state',()=>{
  const input={...message,attachments:[{id:'file',name:'skill.md'}]},run={status:'queued',messages:[input]};
  const before=structuredClone(run),view=queue.presentation(run);
  assert.deepEqual(view.queued,[]);assert.deepEqual(view.transcript,[input]);
  assert.equal(view.transcript[0].attachments[0].name,'skill.md');assert.deepEqual(run,before);
  const b=controller();b.setRun(run);assert.equal(b.element.hidden,true);
});

test('idle follow-ups enter chat despite a completed active ID; only later inputs remain queued',()=>{
  const finished={...active,status:'completed'},reply={id:3,role:'assistant',status:'completed',content:'Done'};
  const next={...message,id:4},later={...message,id:5};
  const run={status:'queued',active_message_id:1,messages:[finished,reply,next,later]};
  let view=queue.presentation(run);
  assert.deepEqual(view.transcript,[finished,reply,next]);assert.deepEqual(view.queued,[later]);
  next.status='running';view=queue.presentation(run);
  assert.deepEqual(view.transcript,[finished,reply,next]);assert.deepEqual(view.queued,[later]);
  next.status='completed';view=queue.presentation(run);
  assert.deepEqual(view.transcript,[finished,reply,next,later]);assert.deepEqual(view.queued,[]);
});

test('dispatch priority matches Send now, while active work retains all genuine queue controls',()=>{
  const first={...message},priority={...message,id:3};
  const run={status:'queued',steer_message_id:3,messages:[first,priority]};
  assert.deepEqual(queue.presentation(run),{queued:[first],transcript:[priority]});
  for(const status of ['queued','provisioning','running','saving','awaiting_approval','waiting_children','waiting_credential','reconnecting']){
    const working={...run,status,messages:[active,first,priority]};
    assert.deepEqual(queue.queued(working),[priority,first]);
    const b=controller();b.setRun(working);assert.match(b.element.html,/2 queued/);assert.match(b.element.html,/Edit queued message/);
  }
  for(const status of ['stopping','deleting','cancelled','interrupted'])assert.deepEqual(queue.queued({...run,status}),[priority,first]);
});

test('a queued edit survives promotion into the conversation before worker claim',async()=>{
  const b=controller();await b.ctrl.act(2,'edit');b.drafts.get(2).content='Keep this correction';
  b.setRun({status:'queued',active_message_id:1,messages:[{...active,status:'completed'},message]});
  assert.doesNotMatch(b.element.html,/data-queued-row="2"/);assert.match(b.element.html,/Unsent edit/);
  assert.equal(b.drafts.get(2).content,'Keep this correction');
  await b.ctrl.act(2,'followup');assert.deepEqual(b.used,['Keep this correction']);assert.equal(b.element.hidden,true);
});

test('queue controls are scoped and show pending, requested, and locked handoff states',()=>{
  const html=queue.card({...message,content:'<img src=x onerror=bad()>'},{},'alice','member',false,false,()=> '');
  assert.match(html,/Send now/);assert.match(html,/Edit queued message/);assert.match(html,/Delete queued message/);assert.doesNotMatch(html,/<img/);
  assert.doesNotMatch(queue.card(message,{},'bob','member',false,false,()=> ''),/data-queue-action/);
  assert.match(queue.card(message,{steer_message_id:2},'alice','member',false,false,()=> ''),/Send requested/);
  assert.doesNotMatch(queue.card({...message,queue_locked:1},{},'alice','admin',false,false,()=> ''),/data-queue-action/);
  assert.deepEqual(queue.queued({steer_message_id:3,messages:[message,{...message,id:3},{...message,id:4,status:'running'}]}).map(m=>m.id),[3,2]);
});

test('edits preserve revision and draft, and save-now submits the saved revision before steering',async()=>{
  const b=controller();await b.ctrl.act(2,'edit');b.drafts.get(2).content='Changed text';const before=b.writes;
  b.ctrl.render({id:'run',messages:[active,{...message}]});assert.equal(b.writes,before,'status refresh must not replace a typing editor');
  await b.ctrl.act(2,'save-now');
  assert.deepEqual(b.requests.map(r=>r.body),[{action:'edit',content:'Changed text',revision:0},{action:'steer',revision:1}]);
  assert.equal(b.drafts.size,0);
});

test('deletion locks queued editors and every direct action despite a stale status refresh',async()=>{
  const b=controller();await b.ctrl.act(2,'edit');b.drafts.get(2).content='Keep this correction';
  b.setRun({status:'deleting',messages:[active,message]});b.setRun({status:'running',messages:[active,message]});
  for(const action of ['edit','save','save-now','steer','delete','followup','discard'])await b.ctrl.act(2,action);
  await b.ctrl.sendFirst();
  assert.equal(b.element.inert,true);assert.equal(b.requests.length,0);assert.equal(b.used.length,0);
  assert.equal(b.drafts.get(2).content,'Keep this correction');
  assert.match(b.element.html,/<textarea[^>]+disabled/);
  assert.match(b.element.html,/data-queue-action="steer"[^>]+disabled/);
});

test('deletion during an awaited queue edit cannot dispatch its follow-on steer',async()=>{
  const b=controller(),requests=[];let finish;
  const c=queue.create({...b.options,api:async(url,options)=>{requests.push(JSON.parse(options.body));await new Promise(resolve=>finish=resolve);return {revision:1};},refresh:async()=>{}});
  c.render({status:'running',messages:[active,message]});await c.act(2,'edit');
  const pending=c.act(2,'save-now');c.render({status:'deleting',messages:[active,message]});finish();await pending;
  assert.deepEqual(requests.map(body=>body.action),['edit']);assert.equal(b.element.inert,true);
});

test('picked-up message keeps an unsaved edit available as a follow-up without mutating history',async()=>{
  const b=controller();await b.ctrl.act(2,'edit');b.drafts.get(2).content='Keep this unsent draft';
  b.setRun({id:'run',messages:[{...message,status:'running'}]});
  assert.match(b.element.html,/Unsent edit/);assert.match(b.element.html,/Keep this unsent draft/);
  await b.ctrl.act(2,'save');assert.equal(b.requests.length,0);assert.equal(b.drafts.size,1);
  await b.ctrl.act(2,'followup');assert.deepEqual(b.used,['Keep this unsent draft']);assert.equal(b.drafts.size,0);
});

test('failed edit retains the text, and delete never submits an edited draft by accident',async()=>{
  const b=controller();b.options.api=async()=>{throw Error('already picked up');};
  // The API callback is captured at construction; use an independent failing controller.
  const drafts=new Map([[2,{content:'Unsent revised text',revision:0}]]),calls=[];
  const c=queue.create({...b.options,drafts,api:async(url,options)=>{calls.push(JSON.parse(options.body));throw Error('already picked up');},refresh:async()=>{}});
  c.render({messages:[active,message]});await c.act(2,'save');assert.equal(drafts.get(2).content,'Unsent revised text');
  await c.act(2,'delete');assert.deepEqual(calls[1],{action:'delete',revision:0});
});

test('composer shortcuts distinguish queue/send-now and leave Shift+Enter, IME, skills, and attachments intact',()=>{
  const script=readFileSync('app/static/app.js','utf8');const handlers={},sent=[];let first=0,hasFiles=false,skillHandles=false;
  const hidden={kind:'now'},submit={disabled:false},form={querySelector:s=>s==='[data-send-now]'?hidden:submit,requestSubmit:button=>sent.push(button?.kind||'queue')};
  const input={id:'followup',value:'Draft',addEventListener:(event,fn)=>handlers[event]=fn};
  const context={state:{selected:'run',messageQueue:{sendFirst:()=>first++}},autoSize:()=>{},bindSkillEditor:input=>input,bindInlineSkillPicker:()=>({keydown:()=>skillHandles}),bindAttachments:()=>({hasFiles:()=>hasFiles})};
  vm.createContext(context);vm.runInContext(script.slice(script.indexOf('function bindComposer'),script.indexOf('async function navigate')),context);context.bindComposer(input,form);
  const key=extra=>handlers.keydown({key:'Enter',preventDefault(){},...extra});
  key({});key({metaKey:true});key({ctrlKey:true});key({shiftKey:true});key({isComposing:true,ctrlKey:true});
  assert.deepEqual(sent,['queue','now','now']);input.value='';key({ctrlKey:true});assert.equal(first,1);
  hasFiles=true;key({metaKey:true});assert.equal(sent.at(-1),'now');assert.equal(first,1);
  skillHandles=true;key({ctrlKey:true});assert.equal(sent.length,4);
  form.inert=true;hasFiles=false;skillHandles=false;key({ctrlKey:true});input.value='Draft';key({});
  assert.equal(first,1);assert.equal(sent.length,4);
});

test('automatic follow-ups appear in the conversation while ordinary messages retain queue controls',()=>{
  const automatic={...message,id:3,send_immediately:1};
  const run={status:'running',messages:[active,message,automatic]};
  assert.deepEqual(queue.presentation(run),{queued:[message],transcript:[active,automatic]});
  const b=controller();let immediate=false;
  const c=queue.create({...b.options,sendImmediately:()=>immediate});
  c.render(run);assert.match(b.element.html,/Enter to queue/);
  immediate=true;c.render(run);assert.match(b.element.html,/New messages send immediately/);
  assert.match(b.element.html,/1 queued/);
  c.render({...run,messages:[active,automatic]});assert.equal(b.element.hidden,true);
  assert.deepEqual(queue.presentation({...run,status:'queued',messages:[message,automatic]}),{queued:[message],transcript:[automatic]});
});
