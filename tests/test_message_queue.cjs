const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const queue=require('../app/static/message-queue.js');
const message={id:2,role:'user',user_id:'alice',user_name:'Alice',status:'queued',content:'Original message',model:'astra',revision:0,queue_locked:0,attachments:[]};

function controller(){
  const requests=[],toasts=[],drafts=new Map(),used=[];let run={id:'run',messages:[{...message}]},writes=0;
  const element={ownerDocument:{},hidden:true,querySelectorAll:()=>[],querySelector:()=>null,set innerHTML(text){this.html=text;writes++;}};
  const options={element,runId:'run',user:'alice',role:'member',drafts,toast:value=>toasts.push(value),useDraft:text=>used.push(text),api:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {revision:1};},refresh:async()=>ctrl.render(run)};
  const ctrl=queue.create(options);ctrl.render(run);
  return {ctrl,requests,toasts,drafts,used,element,options,get writes(){return writes;},setRun(next){run=next;ctrl.render(next);}};
}

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
  b.ctrl.render({id:'run',messages:[{...message}]});assert.equal(b.writes,before,'status refresh must not replace a typing editor');
  await b.ctrl.act(2,'save-now');
  assert.deepEqual(b.requests.map(r=>r.body),[{action:'edit',content:'Changed text',revision:0},{action:'steer',revision:1}]);
  assert.equal(b.drafts.size,0);
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
  c.render({messages:[message]});await c.act(2,'save');assert.equal(drafts.get(2).content,'Unsent revised text');
  await c.act(2,'delete');assert.deepEqual(calls[1],{action:'delete',revision:0});
});

test('composer shortcuts distinguish queue/send-now and leave Shift+Enter, IME, skills, and attachments intact',()=>{
  const script=readFileSync('app/static/app.js','utf8');const handlers={},sent=[];let first=0,hasFiles=false,skillHandles=false;
  const hidden={kind:'now'},submit={disabled:false},form={querySelector:s=>s==='[data-send-now]'?hidden:submit,requestSubmit:button=>sent.push(button?.kind||'queue')};
  const input={id:'followup',value:'Draft',addEventListener:(event,fn)=>handlers[event]=fn};
  const context={state:{selected:'run',messageQueue:{sendFirst:()=>first++}},autoSize:()=>{},bindInlineSkillPicker:()=>({keydown:()=>skillHandles}),bindAttachments:()=>({hasFiles:()=>hasFiles})};
  vm.createContext(context);vm.runInContext(script.slice(script.indexOf('function bindComposer'),script.indexOf('async function navigate')),context);context.bindComposer(input,form);
  const key=extra=>handlers.keydown({key:'Enter',preventDefault(){},...extra});
  key({});key({metaKey:true});key({ctrlKey:true});key({shiftKey:true});key({isComposing:true,ctrlKey:true});
  assert.deepEqual(sent,['queue','now','now']);input.value='';key({ctrlKey:true});assert.equal(first,1);
  hasFiles=true;key({metaKey:true});assert.equal(sent.at(-1),'now');assert.equal(first,1);
  skillHandles=true;key({ctrlKey:true});assert.equal(sent.length,4);
});
