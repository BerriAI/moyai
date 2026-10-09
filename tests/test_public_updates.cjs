const assert=require('node:assert/strict');
require('./helpers/ui-vm.cjs');
const {test}=require('node:test');
const {groups,updates,completedHistory,updateHTML,html,sync}=require('../app/static/activity.js');
const stamp=n=>new Date(Date.UTC(2026,9,1,12,0,n)).toISOString();
const event=(id,kind,message,data={})=>({id,kind,message,data,created_at:stamp(id)});
const answer=(id=4,status='completed')=>({id,role:'assistant',status,content:'The change is saved.',created_at:stamp(id)});
function run(){return {status:'running',active_message_id:1,messages:[
  {id:1,role:'user',status:'running'},{id:2,role:'user',status:'injected',steering_parent_id:1},
  {id:3,role:'user',status:'queued'}],events:[
  event(1,'chat','Response started',{message_id:1}),
  event(2,'message','I will inspect the original issue.'),
  event(3,'chat','Message queued',{message_id:3}),
  event(4,'message','Original work continues while another input waits.'),
  event(5,'status','Your message is guiding the current task.',{turn_id:1,message_id:2,phase:'steering'}),
  event(6,'message','The merged PR changes costs, but **daily accounting is still needed**.',{turn_id:1,activity_id:'reply-1',phase:'commentary'}),
  event(7,'status','Preparing the next step',{phase:'processing'})]};}

test('updates follow delivery receipts and remain separate from tool details',()=>{
  const data=run();for(let i=8;i<30;i++)data.events.push(event(i,'tool','Read file',{turn_id:1}));
  const buckets=updates(data),turn=groups(data).get('1');
  assert.equal(buckets.get('1').length,2);assert.equal(buckets.get('2').length,1);assert.equal(buckets.has('3'),false);
  assert.match(buckets.get('2')[0].content,/daily accounting/);
  const toolHTML=html(turn);assert.match(toolHTML,/earlier updates/);assert.doesNotMatch(toolHTML,/daily accounting|Original work continues/);
  const visible=updateHTML(buckets.get('2')[0]);
  assert.match(visible,/<article class="chat-message assistant assistant-update"/);assert.match(visible,/daily accounting/);
  assert.doesNotMatch(visible,/<details/);assert.match(visible,/Copy update/);
  data.messages[0].status='completed';data.status='idle';data.events.push(event(30,'chat','Response saved',{message_id:1}));
  assert.deepEqual(updates(data),buckets,'completed work must retain every update in its history');
});

test('only successful closeouts collapse progress, with steering replies under their own input',()=>{
  const data=run();
  assert.equal(completedHistory(data).size,0);
  for(const status of ['failed','cancelled','interrupted','steered','save_failed','saving','awaiting_approval','waiting_credential','waiting_children']){
    data.status=status;data.messages[0].status=status;
    assert.equal(completedHistory(data).size,0,status);
  }
  data.status='idle';data.messages[0].status='completed';
  data.messages.push(answer());
  data.events.push(event(30,'chat','Response saved',{message_id:1}));
  assert.deepEqual([...completedHistory(data)],[['1','Earlier activity'],['2','Worked for 29s']]);
  assert.equal(completedHistory(data).has('3'),false,'a queued input has no completed history');
  assert.deepEqual(completedHistory(structuredClone(data)),completedHistory(data),'reload reconstructs history');
});

test('save and idle events keep progress visible until the final reply reaches the transcript',()=>{
  for(const signal of ['saved','idle','completed message']){
    const data=run(),view=dom();sync(view.container,data,view.options);
    const update=view.slots[1].children[0];
    if(signal==='saved')data.events.push(event(30,'chat','Response saved',{message_id:1}));
    if(signal==='idle')data.status='idle';
    if(signal==='completed message')data.messages[0].status='completed';
    assert.equal(completedHistory(data).size,0,signal);
    sync(view.container,data,view.options);
    assert.equal(view.slots[1].children[0],update,'the update stays visible before the answer');
    data.messages[0].status='completed';data.messages.push(answer());
    sync(view.container,data,view.options);
    assert.equal(view.slots[1].children[0].className,'turn-work completed-work');
    assert.equal(view.slots[1].children[0].querySelector('.work-body').children[0],update);
  }
});

test('a previous answer and queued inputs cannot close a later turn without its own final reply',()=>{
  const data=run();data.messages[0].status='completed';
  data.messages=[data.messages[0],data.messages[1],answer(),{...data.messages[2],status:'running'}];
  data.active_message_id=3;
  data.events.push(event(30,'chat','Response saved',{message_id:1}),event(31,'chat','Response started',{message_id:3}),
    event(32,'message','Checking the follow-up.',{turn_id:3}),event(33,'chat','Response saved',{message_id:3}));
  assert.equal(completedHistory(data).has('1'),true);
  assert.equal(completedHistory(data).has('3'),false,'the first answer is not the follow-up answer');
  data.messages.push({id:5,role:'user',status:'queued',send_immediately:true},answer(6));
  data.messages[3].status='completed';
  assert.equal(completedHistory(data).has('3'),true,'a queued input cannot claim the preceding turn answer');
  assert.equal(completedHistory(data).has('5'),false);
  assert.deepEqual(completedHistory(structuredClone(data)),completedHistory(data));
});

test('only a visible successful answer closes progress, including interleaved session metadata replies',()=>{
  const data=run();data.status='idle';data.messages[0].status='completed';
  for(const status of ['steered','queued','failed','cancelled','interrupted','save_failed']){
    data.messages.push(answer(4,status));
    assert.equal(completedHistory(data).size,0,status);
    data.messages.pop();
  }
  data.messages.push({id:4,role:'user',status:'cancelled',started_at:''},
    {id:5,role:'user',status:'completed',started_at:stamp(10)},answer(6));
  assert.equal(completedHistory(data).has('1'),false,'an inline session-ID reply is not the task answer');
  data.messages.push(answer(7));
  assert.equal(completedHistory(data).has('1'),true,'the task answer follows the inline reply');
  assert.equal(completedHistory(data).has('2'),true,'delivered steering shares the task answer');
  assert.equal(completedHistory(data).has('3'),false);
  assert.equal(completedHistory(data).has('4'),false,'a cancelled queued input has no answer');
});

test('multiple steering inputs, replay, and a later turn retain the correct update order',()=>{
  const data=run();
  data.events.push(event(8,'message',data.events[5].message,{turn_id:1,activity_id:'reply-1'}));
  data.messages.push({id:4,role:'user',status:'injected',steering_parent_id:1});
  data.events.push(event(9,'status','Delivery confirmed',{turn_id:1,message_id:4,phase:'steering'}),
    event(10,'message','Answer to the second correction.',{turn_id:1}),
    event(11,'chat','Response saved',{message_id:1}),event(12,'chat','Response started',{message_id:3}),
    event(13,'message','Now handling the queued next turn.',{turn_id:3}));
  const buckets=updates(data);
  assert.equal(buckets.get('2').length,1,'a replayed activity ID is displayed once');
  assert.equal(buckets.get('4')[0].content,'Answer to the second correction.');
  assert.equal(buckets.get('3')[0].content,'Now handling the queued next turn.');
  assert.deepEqual(updates(JSON.parse(JSON.stringify(data))),buckets,'reload uses saved history, not local delivery state');
});

test('a streamed receipt before the refreshed input does not misplace its reply under the old input',()=>{
  const data=run();data.messages=data.messages.filter(message=>message.id!==2);
  assert.equal(updates(data).get('1').length,2);
  assert.match(updates(data).get('2')[0].content,/merged PR/);
});

test('only public messages become updates and their Markdown uses the same trusted renderer as replies',()=>{
  const data=run();data.events.push(event(8,'message','',{}),event(9,'message','not commentary',{phase:'processing'}),
    event(10,'tool','Run command',{detail:{output:'private tool body'}}),event(11,'reasoning','private reasoning',{}));
  assert.equal([...updates(data).values()].flat().length,3);
  const content='<img src=x onerror=alert(1)> [click](javascript:alert(1))';
  assert.doesNotMatch(updateHTML({id:'unsafe"<',content}),/<img src=x|data-update-id="unsafe"</);
  let received;
  const markup=updateHTML({id:'a',content:'**Verified**'},text=>{received=text;return '<p><strong>Verified</strong></p>';});
  assert.equal(received,'**Verified**');assert.match(markup,/<strong>Verified<\/strong>/);
});

// Minimal DOM for checking identity/focus preservation; real Markdown and
// layout are also verified in the browser using the production renderer.
function dom(){
  let creations=0;const copied=[];
  function container(dataset={}){
    return {dataset,ownerDocument:document,children:[],
      contains(node){return node===this||this.children.some(child=>child===node||child.contains?.(node));},
      querySelector(selector){return selector===':scope > .completed-work'?this.children.find(child=>child.className==='turn-work completed-work'):null;},
      querySelectorAll(){return this.children;},
      append(...nodes){for(const node of nodes)this.insertBefore(node,null);},
      insertBefore(node,before){if(node.parent)node.remove();const i=before?this.children.indexOf(before):this.children.length;this.children.splice(i,0,node);node.parent=this;},
      remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
    };
  }
  const document={createElement(tag){
    if(tag==='details'){
      const node=container(),body=container(),title={},summary={focus:()=>{document.activeElement=summary;}};
      node.open=false;node.querySelector=selector=>({'.work-body':body,'.work-title':title,summary}[selector]);
      return node;
    }
    if(tag==='div')return {dataset:{},ownerDocument:document,contains:()=>false,querySelectorAll:()=>[],innerHTML:'',remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}};
    return {content:{},set innerHTML(markup){
      creations++;
      const button={},codeButton={closest:()=>({querySelector:()=>({textContent:'example code'})})};
      const node={dataset:{updateId:markup.match(/data-update-id="([^"]+)/)[1]},markup,button,codeButton,
        querySelector:()=>button,querySelectorAll:()=>[codeButton],
        replaceWith(next){const i=this.parent.children.indexOf(this);this.parent.children[i]=next;next.parent=this.parent;},
        remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}};
      this.content.firstElementChild=node;
    }};
  }};
  const slots=['1','2','3'].map(id=>container({activitySlot:id}));
  const view={scrollTop:50,scrollHeight:1000,clientHeight:400,querySelectorAll:selector=>selector==='[data-activity-slot]'?slots:[]};
  return {slots,container:view,document,copied,options:{copy:(text,button)=>copied.push(text)},get creations(){return creations;}};
}

test('completion folds existing updates once; expansion, copy and focus survive polling',()=>{
  const data=run(),view=dom();sync(view.container,data,view.options);
  const update=view.slots[1].children[0];view.document.activeElement=update;
  data.messages[0].status='completed';data.status='idle';data.events.push(event(30,'chat','Response saved',{message_id:1}));
  data.messages.push(answer());
  sync(view.container,data,view.options);
  const history=view.slots[1].children[0];
  assert.equal(history.open,false);assert.equal(history.querySelector('.work-title').textContent,'Worked for 29s');
  assert.equal(view.document.activeElement,history.querySelector('summary'),'focus leaves collapsed content');
  assert.equal(history.querySelector('.work-body').children[0],update);
  history.open=true;update.button.onclick();
  assert.deepEqual(view.copied,[data.events[5].message]);
  sync(view.container,structuredClone(data),view.options);
  assert.equal(view.slots[1].children[0],history);assert.equal(history.open,true);
  assert.equal(view.container.scrollTop,50);assert.equal(view.creations,3);
  data.messages[0].status='save_failed';data.status='failed';
  sync(view.container,data,view.options);
  assert.equal(view.slots[1].children[0],update,'a later save failure exposes the same progress again');
});

test('new tools and replies preserve existing update nodes, reading position, and copy controls',()=>{
  const data=run(),view=dom();sync(view.container,data,view.options);
  const answer=view.slots[1].children[0];assert.equal(view.creations,3);
  answer.selectedPassage='daily accounting';answer.button.onclick();answer.codeButton.onclick();
  assert.deepEqual(view.copied,[data.events[5].message,'example code']);
  data.events.push(event(8,'tool','Read another file',{turn_id:1}));sync(view.container,data,view.options);
  assert.equal(view.creations,3);assert.equal(view.slots[1].children[0],answer);
  data.events.push(event(9,'message','Another visible update.',{turn_id:1}));sync(view.container,data,view.options);
  assert.equal(view.creations,4);assert.equal(view.slots[1].children[0],answer);
  assert.equal(answer.selectedPassage,'daily accounting');assert.equal(view.container.scrollTop,50);
  sync(view.container,structuredClone(data),view.options);assert.equal(view.creations,4);
});
