const assert=require('node:assert/strict');
const {test}=require('node:test');
const {store,messageHTML,publicItems}=require('../app/static/swarm-chat.js');
const root='a'.repeat(32),child='b'.repeat(32);
const item=(id,status='completed',content=id)=>({id,run_id:child,kind:'assistant',content,status,created_at:'2026-10-10T10:00:00Z',author:{id:child,name:'Builder',role:'agent',harness:'codex',model:'example/model'}});
const page=(items,extra={})=>({items,live_items:[],next_cursor:'tail',previous_cursor:'head',has_more:false,...extra});
function setup(responses,options={}){const calls=[],changes=[];let time=0;const feed=store({runId:root,api:async(url,opts)=>{calls.push({url,opts});const value=responses.shift();if(value instanceof Error)throw value;return value;},now:()=>time,onChange:(state,mode)=>changes.push({state,mode}),...options});return {feed,calls,changes,tick:()=>{time+=9000;}};}

test('latest and incremental pages deduplicate, watch pending replies and apply final upserts',async()=>{
 const current=item('response:one','saving','Public partial'),final=item('response:one','completed','Public final');
 const {feed,calls,tick}=setup([page([current],{next_cursor:'opaque+tail',live_items:[{...current,content:'New partial'}]}),page([],{updated_items:[final],removed_ids:[]})]);
 await feed.poll();assert.equal(feed.snapshot().items[0].content,'New partial');tick();await feed.poll();
 const query=new URL(calls[1].url,'https://test').searchParams;
 assert.equal(query.get('after'),'opaque+tail');assert.equal(query.get('watch'),'response:one');
 assert.deepEqual(feed.snapshot().items,[final]);feed.dispose();
});

test('tombstones remove optimistic and pending messages; injected human messages are immediate and bounded',async()=>{
 const {feed,tick}=setup([page([]),page([],{removed_ids:['message:steered']})]);await feed.poll();
 feed.inject([item('message:steered','injected')]);assert.equal(feed.snapshot().items.length,1);
 tick();await feed.poll();assert.equal(feed.snapshot().items.length,0);
 feed.inject(Array.from({length:80},(_,i)=>item('local:'+i)));assert.equal(feed.snapshot().items.length,50);feed.dispose();
});

test('history freezes polling, preserves tail cursor and keeps a bounded window',async()=>{
 const responses=Array.from({length:7},(_,index)=>page(Array.from({length:50},(_,i)=>item('p'+index+':'+i)),{has_more:true,next_cursor:'tail'+index,previous_cursor:'head'+index}));
 const {feed,calls,tick}=setup(responses);await feed.poll();await feed.earlier();
 assert.equal(new URL(calls[1].url,'https://test').searchParams.get('before'),'head0');
 tick();await feed.poll();assert.equal(calls.length,2);
 for(let i=0;i<4;i++)await feed.earlier();assert.equal(feed.snapshot().items.length,200);
 await feed.latest();assert.equal(feed.snapshot().items.length,50);assert.equal(feed.snapshot().history,false);feed.dispose();
});

test('long active feeds retain at most four durable pages and one hundred public live replies',async()=>{
 const responses=Array.from({length:8},(_,index)=>page(Array.from({length:50},(_,i)=>item(index+':'+i)),{live_items:Array.from({length:150},(_,i)=>item('live:'+i,'saving'))}));
 const {feed,tick}=setup(responses);for(let i=0;i<8;i++){await feed.poll();tick();}
 assert.equal(feed.snapshot().items.length,300);assert.equal(feed.snapshot().older,true);feed.dispose();
});

test('hidden, paused and disposed requests cannot repaint stale routes or overlap',async()=>{
 let resolve,active=true;const calls=[];const feed=store({runId:root,api:(url,opts)=>{calls.push(opts);return new Promise(r=>resolve=r);},active:()=>active});
 const first=feed.poll();await feed.poll();assert.equal(calls.length,1);feed.pause();assert.equal(calls[0].signal.aborted,true);
 resolve(page([item('stale')]));await first;assert.deepEqual(feed.snapshot().items,[]);
 active=false;await feed.poll();assert.equal(calls.length,1);active=true;const second=feed.poll();feed.dispose();resolve(page([item('disposed')]));await second;assert.deepEqual(feed.snapshot().items,[]);
});

test('temporary failures keep messages for retry; revoked access clears all messages',async()=>{
 const transient=Object.assign(new Error('temporary'),{status:503}),revoked=Object.assign(new Error('revoked'),{status:403});
 const {feed,tick}=setup([page([item('saved')]),transient,page([item('next')]),revoked]);
 await feed.poll();tick();await feed.poll();assert.equal(feed.snapshot().items.length,1);assert.match(feed.snapshot().error,/refresh/);
 await feed.retry();assert.equal(feed.snapshot().error,'');tick();await feed.poll();assert.deepEqual(feed.snapshot().items,[]);assert.equal(feed.snapshot().denied,true);feed.dispose();
});

test('room renders public speech, harness identity, lead and targets with escaped metadata',()=>{
 global.MoyaiProviderLogos={harness:()=>'/static/codex.svg'};
 const html=messageHTML({...item('reply'),author:{...item('reply').author,name:'Builder <script>',is_leader:true},target:{id:root,name:'Coordinator'},kind:'assignment',truncated:true,status:'save_failed'},{harnessName:()=> 'Codex',modelName:()=> 'Model'});
 assert.match(html,/Builder &lt;script&gt;/);assert.doesNotMatch(html,/<script>/);assert.match(html,/src="\/static\/codex.svg"/);assert.match(html,/Lead/);assert.match(html,/Codex/);assert.match(html,/Assignment/);assert.match(html,/Read full message/);assert.match(html,/workspace save failed/);
 assert.equal(publicItems([item('valid'),{...item('bad'),kind:'reasoning'},{...item('bad2'),run_id:'javascript:alert(1)'}]).length,1);
 delete global.MoyaiProviderLogos;
});

test('a reply that was only live remains visible when it settles between polls',async()=>{
 const pending=item('response:old','saving','Earlier public partial'),final={...pending,status:'completed',content:'Earlier final reply'};
 const {feed,tick}=setup([page([item('newer')],{live_items:[pending]}),page([],{updated_items:[final],live_items:[]})]);
 await feed.poll();tick();await feed.poll();assert.deepEqual(feed.snapshot().items.find(item=>item.id===pending.id),final);feed.dispose();
});

test('watch batches rotate so more than one hundred pending replies eventually settle',async()=>{
 const first=Array.from({length:50},(_,i)=>item('response:'+i,'saving'));
 const live=Array.from({length:100},(_,i)=>item('response:'+(i+50),'saving'));
 const {feed,calls,tick}=setup([page(first,{live_items:live}),page([],{live_items:live}),page([],{live_items:live})]);
 await feed.poll();tick();await feed.poll();tick();await feed.poll();
 const watched=new Set(calls.slice(1).flatMap(call=>new URL(call.url,'https://test').searchParams.get('watch').split(',')));
 assert.equal(watched.size,150);feed.dispose();
});
