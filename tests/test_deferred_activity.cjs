const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
const Activity=require('../app/static/activity.js');
const flush=()=>new Promise(resolve=>setImmediate(resolve));

// A small native disclosure double. Activity's rich markup is covered by the
// existing activity suite and real browser checks; these assertions exercise
// disclosure fetch timing, replacement, keyboard focus and retry behavior.
function disclosureFixture(){
  const doc={activeElement:null,createElement:tag=>new Element(tag)};
  class Element{
    constructor(tag){this.tag=tag;this.dataset={};this.children=[];this.ownerDocument=doc;this.listeners={};this.open=false;}
    append(...nodes){for(const node of nodes){node.remove();node.parent=this;this.children.push(node);}}
    insertBefore(node,before){node.remove();node.parent=this;this.children.splice(before?this.children.indexOf(before):this.children.length,0,node);}
    replaceChildren(...nodes){for(const node of [...this.children])node.remove();this.append(...nodes);}
    remove(){if(this.parent){this.parent.children.splice(this.parent.children.indexOf(this),1);this.parent=null;}}
    contains(node){return node===this||this.children.some(child=>child.contains(node));}
    addEventListener(type,callback){this.listeners[type]=callback;}
    focus(){doc.activeElement=this;}
    matches(selector){
      if(selector==='[data-work-key]')return !!this.dataset.workKey;
      if(selector==='[data-deferred-activity]')return !!this.dataset.deferredActivity;
      return selector.startsWith('.')?this.className?.split(' ').includes(selector.slice(1)):this.tag===selector;
    }
    querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);}
    querySelector(selector){return selector.startsWith(':scope > ')?this.children.find(child=>child.matches(selector.slice(9))):this.querySelectorAll(selector)[0];}
    set innerHTML(markup){
      this.replaceChildren();this.markup=markup;
      if(markup.includes('data-work-key=')){
        const details=new Element('details');details.dataset.workKey=markup.match(/data-work-key="([^"]+)"/)[1];
        details.innerHTML='<summary class="work-heading"></summary><div class="work-body"></div>';this.append(details);
      }else if(markup.includes('<summary')){
        const summary=new Element('summary'),title=new Element('span'),body=new Element('div');
        summary.className='work-heading';title.className='work-title';body.className='work-body';summary.append(title);this.append(summary,body);
      }
    }
  }
  const slot=new Element('div');slot.dataset.activitySlot='1';
  const container={scrollHeight:1000,scrollTop:50,clientHeight:400,querySelectorAll:()=>[slot]};
  const run={id:'fixture',status:'idle',active_message_id:null,messages:[{id:1,role:'user',status:'completed'},{id:2,role:'assistant',status:'completed',content:'Done'}],
    events:[{id:1,kind:'chat',message:'Response started',data:{message_id:1},created_at:'2026-10-09T10:00:00Z'},
      {id:3,kind:'chat',message:'Response saved',data:{message_id:1},created_at:'2026-10-09T10:00:10Z'}],deferred_activity:['1'],loaded_activity:[]};
  const tool={id:2,kind:'tool',message:'Read repository',data:{turn_id:1,activity_version:1,call_id:'read',phase:'completed',category:'tool'},created_at:'2026-10-09T10:00:05Z'};
  return {doc,slot,container,run,tool};
}

test('deferred work stays collapsed without fetching, reuses its disclosure, and restores expansion and focus after loading',async()=>{
  const f=disclosureFixture();let resolve,calls=0;
  const options={loadActivity:()=>{calls++;return new Promise(done=>resolve=done);}};
  Activity.sync(f.container,f.run,options);const placeholder=f.slot.children[0];
  assert.equal(calls,0);assert.equal(placeholder.open,false);assert.match(placeholder.querySelector('.work-title').textContent,/Worked for 10s/);
  Activity.sync(f.container,f.run,options);assert.equal(f.slot.children[0],placeholder);
  placeholder.open=true;placeholder.querySelector('summary').focus();
  const opening=placeholder.listeners.toggle();await placeholder.listeners.toggle();
  assert.equal(calls,1);assert.match(placeholder.querySelector('.work-body').textContent,/Loading work history/);
  f.run.events.splice(1,0,f.tool);f.run.loaded_activity=['1'];Activity.sync(f.container,f.run,options);
  const details=f.slot.querySelector('details');assert.notEqual(details,placeholder);
  assert.equal(details.open,true);assert.equal(details.dataset.workManual,'open');
  assert.equal(f.doc.activeElement,details.querySelector('summary'));assert.equal(f.container.scrollTop,50);
  resolve();await opening;assert.equal(calls,1);
});

test('a failed disclosure request remains visible and closing then reopening retries it',async()=>{
  const f=disclosureFixture();let calls=0;
  const options={loadActivity:async()=>{calls++;throw Error('Offline');}};
  Activity.sync(f.container,f.run,options);const placeholder=f.slot.children[0];placeholder.open=true;
  await placeholder.listeners.toggle();assert.match(placeholder.querySelector('.work-body').textContent,/Could not load work history/);
  assert.equal(placeholder.dataset.loading,undefined);
  placeholder.open=false;await placeholder.listeners.toggle();assert.equal(calls,1);
  placeholder.open=true;await placeholder.listeners.toggle();assert.equal(calls,2);
});

test('an already loaded live turn keeps its work when it becomes deferred history after completion',()=>{
  const f=disclosureFixture();f.run.loaded_activity=['1'];f.run.status='running';f.run.messages[0].status='running';
  f.run.events=[f.run.events[0],f.tool];let calls=0;
  Activity.sync(f.container,f.run,{loadActivity:()=>calls++});const work=f.slot.children[0];
  f.run.status='idle';f.run.messages[0].status='completed';
  Activity.sync(f.container,f.run,{loadActivity:()=>calls++});
  assert.equal(f.slot.children[0],work);assert.equal(f.slot.querySelector('[data-deferred-activity]'),undefined);assert.equal(calls,0);
});

test('restored Activity activates after the controller snapshot is installed and activation can retry errors',async()=>{
  const source=readFileSync('app/static/workspace-panel.js','utf8'),calls=[],errors=[];
  let live=true,snapshot=null;
  const context={good:()=>live,activity:{},onActivity:async()=>{calls.push(snapshot);if(calls.length===1)throw Error('Retry history');},toast:message=>errors.push(message)};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('    async function mount(t)'),source.indexOf('    function mountCaptures(t)')),context);
  const tab={kind:'activity',element:{append(){}}};await context.mount(tab);
  const first=tab.activate();snapshot={id:'installed'};assert.equal(calls.length,0);
  await first;assert.equal(calls[0],snapshot);assert.deepEqual(errors,['Retry history']);
  await tab.activate();assert.equal(calls.length,2);
  const stale=tab.activate();live=false;await stale;await flush();assert.equal(calls.length,2);
});
