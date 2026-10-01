const assert=require('node:assert/strict');
const {test}=require('node:test');
const {groups,updates,updateHTML,html,sync}=require('../app/static/activity.js');
const stamp=n=>new Date(Date.UTC(2026,9,1,12,0,n)).toISOString();
const event=(id,kind,message,data={})=>({id,kind,message,data,created_at:stamp(id)});
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

test('updates follow delivery receipts and remain visible outside collapsed tool details',()=>{
  const data=run();for(let i=8;i<30;i++)data.events.push(event(i,'tool','Read file',{turn_id:1}));
  const buckets=updates(data),turn=groups(data).get('1');
  assert.equal(buckets.get('1').length,2);assert.equal(buckets.get('2').length,1);assert.equal(buckets.has('3'),false);
  assert.match(buckets.get('2')[0].content,/daily accounting/);
  const toolHTML=html(turn);assert.match(toolHTML,/earlier updates/);assert.doesNotMatch(toolHTML,/daily accounting|Original work continues/);
  const visible=updateHTML(buckets.get('2')[0]);
  assert.match(visible,/<article class="chat-message assistant assistant-update"/);assert.match(visible,/daily accounting/);
  assert.doesNotMatch(visible,/<details/);assert.match(visible,/Copy update/);
  data.messages[0].status='completed';data.status='idle';data.events.push(event(30,'chat','Response saved',{message_id:1}));
  assert.deepEqual(updates(data),buckets,'completed work must keep its visible updates');
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
  const document={createElement(){
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
  const slots=['1','2','3'].map(id=>({dataset:{updateSlot:id},ownerDocument:document,children:[],
    querySelectorAll(){return this.children;},insertBefore(node,before){
      this.children=this.children.filter(child=>child!==node);
      const i=before?this.children.indexOf(before):this.children.length;this.children.splice(i,0,node);node.parent=this;
    }}));
  const container={scrollTop:50,scrollHeight:1000,clientHeight:400,querySelectorAll:selector=>selector==='[data-update-slot]'?slots:[]};
  return {slots,container,copied,options:{copy:(text,button)=>copied.push(text)},get creations(){return creations;}};
}

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
