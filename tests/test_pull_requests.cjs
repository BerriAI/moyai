const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
const panelPath=process.env.PANEL_SOURCE||'app/static/workspace-panel.js';
const {prUrl,restore}=require(require('node:path').resolve(panelPath));
const url='https://github.com/BerriAI/moyai/pull/145';
test('panel defaults leave most space for chat and preserve saved resize choices',()=>{
  for(const value of [null, '{', '{}', JSON.stringify({width:0})])
    assert.equal(restore(value).width,40);
  for(const [width,expected] of [[30,30],[48,48],[60,60],[70,70],[10,30],[90,70]])
    assert.equal(restore(JSON.stringify({width})).width,expected);
});

test('only canonical PR destinations survive tab restore',()=>{
  for(const repository of ['moyai','.github','_config','-tools','Mixed.Case-repo_1']){
    const accepted=url.replace('moyai',repository);
    assert.equal(prUrl(accepted),accepted);
    assert.equal(restore(JSON.stringify({tabs:[{id:'pr:'+accepted,kind:'pr',url:accepted}]})).tabs[0].url,accepted);
  }
  for(const bad of ['javascript:alert(1)',url+'/files',url+'?token=x',url+'#x',url.replace('github.com','github.com.evil.test'),url.replace('145','0'),url.replace('moyai','x'.repeat(512)),'https://user@github.com/BerriAI/moyai/pull/145']){
    assert.equal(prUrl(bad),null,bad);
    assert.deepEqual(restore(JSON.stringify({tabs:[{id:'pr:unsafe',kind:'pr',url:bad}]})).tabs,[]);
  }
  assert.equal(restore(JSON.stringify({tabs:[{id:'pr:'+url,kind:'pr',url}]})).tabs[0].url,url);
});
test('saved captures restore as a session tab with their selected state',()=>{
  const saved=restore(JSON.stringify({visible:true,active:'captures',tabs:[
    {id:'computer',kind:'computer',title:'Computer'},
    {id:'captures',kind:'captures',title:'Saved captures'}
  ]}));
  assert.deepEqual(saved.tabs.map(tab=>tab.kind),['computer','captures']);
  assert.equal(saved.active,'captures');assert.equal(saved.visible,true);
});
test('only session PR links in the owning chat use the native panel; modified and embedded links stay native',()=>{
  const source=fs.readFileSync(panelPath,'utf8'),opened=[];
  const ctx={prUrl,openPullRequest:link=>{if(link!==url)return false;opened.push(link);return true;}};
  vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    function followPullRequest('),source.indexOf("    layout.addEventListener('click',followPullRequest)")),ctx);
  let prevented=0;const event=(href=url,scope=true,extra={})=>({target:{closest:()=>({closest:()=>scope,getAttribute:()=>href})},preventDefault(){prevented++;},...extra});
  ctx.followPullRequest(event());assert.deepEqual(opened,[url]);assert.equal(prevented,1);
  for(const modifier of ['ctrlKey','metaKey','shiftKey','altKey'])ctx.followPullRequest(event(url,true,{[modifier]:true}));
  ctx.followPullRequest(event(url,false));ctx.followPullRequest(event(url.replace('145','999')));ctx.followPullRequest(event(url,true,{button:1}));
  assert.equal(opened.length,1);assert.equal(prevented,1);
});
function closingPanel({active='pr:a',mode='modal'}={}){
  const events=[],tabs=new Map();
  for(const id of ['pr:a','pr:b'])tabs.set(id,{id,kind:'pr',url:id==='pr:a'?url:url.replace('145','135'),loaded:true,
    element:{hidden:id!==active,remove(){events.push('remove:'+id);}},
    activate(){events.push('activate:'+id);},deactivate(){events.push('deactivate:'+id);}});
  const closingStatus={hidden:true};
  const ctx={tabs,active,visible:true,disposed:false,closingStatus,run:{id:'session',mode},
    computer:{closeTab(id,tab){events.push('close:'+tab);throw Error('Native PR close must not use Computer');}},
    draw(){},save(){},toast:message=>events.push(message),q:()=>({scrollIntoView(){}}),
    document:{querySelector:()=>null},setVisible(value){ctx.visible=value;},
    pullRequests:[{url,title:'Review'}],make:()=>tabs.get('pr:a')};
  const source=fs.readFileSync(panelPath,'utf8');
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('    function select('),source.indexOf('    function error(')),ctx);
  vm.runInContext(source.slice(source.indexOf('    function hide('),source.indexOf('    function menu(')),ctx);
  vm.runInContext(source.slice(source.indexOf('    function openPullRequest('),source.indexOf('    function syncPullRequests(')),ctx);
  return {ctx,tabs,events,closingStatus};
}
test('cloud PR close is local and never asks the computer to close a browser',async()=>{
  for(const hidden of [false,true]){
    const f=closingPanel();if(hidden)f.ctx.hide();
    await f.ctx.remove('pr:a');
    assert.equal(f.events.some(e=>e.startsWith('close:')),false);
    assert.equal(f.tabs.has('pr:a'),false);assert.equal(f.ctx.active,'pr:b');
    assert.equal(f.ctx.visible,!hidden);
    assert.equal(f.events.includes('activate:pr:b'),!hidden);
    await f.ctx.remove('pr:b');assert.equal(f.tabs.size,0);assert.equal(f.ctx.visible,false);
  }
});
test('closing an inactive PR preserves the selected file tab',async()=>{
  const f=closingPanel();
  f.tabs.set('files',{id:'files',kind:'files',loaded:true,element:{hidden:true},activate(){f.events.push('activate:files');}});
  f.ctx.select('files');await f.ctx.remove('pr:a');
  assert.equal(f.ctx.active,'files');assert.equal(f.ctx.visible,true);
  assert.equal(f.events.filter(e=>e==='activate:files').length,1);
  assert.equal(f.events.some(e=>e.startsWith('close:')),false);
});
test('PR mount delegates to native details for every run mode without touching Computer',async()=>{
  for(const mode of ['modal','demo']){
    const source=fs.readFileSync(panelPath,'utf8'),calls=[],element={};
    const ctx={run:{id:'session',mode},good:()=>true,markdown:String,esc:String,encodeURIComponent,
      api:async path=>{calls.push(path);return {};},
      computer:new Proxy({},{get(){throw Error('PR viewing must never access Computer');}}),
      MoyaiPullRequest:{mount(options){assert.equal(options.element,element);return {activate:options.load};}}};
    vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    async function mount('),source.indexOf('    function mountCaptures(')),ctx);
    const tab={kind:'pr',url,element};await ctx.mount(tab);await tab.activate();
    assert.deepEqual(calls,['/api/runs/session/pull-request?url='+encodeURIComponent(url)]);
  }
});
test('switching between live views and captures runs each owning tab lifecycle',()=>{
  for(const id of ['computer','pr:a']){
    const f=closingPanel({active:id});
    if(id==='computer')f.tabs.set(id,{id,kind:'computer',loaded:true,element:{hidden:false},
      activate(){f.events.push('activate:'+id);},deactivate(){f.events.push('deactivate:'+id);}});
    f.tabs.set('captures',{id:'captures',kind:'captures',loaded:true,element:{hidden:true},
      activate(){f.events.push('activate:captures');},deactivate(){f.events.push('deactivate:captures');}});
    f.ctx.select('captures');
    assert.equal(f.ctx.active,'captures');assert.equal(f.tabs.get(id).element.hidden,true);
    assert.deepEqual(f.events,['deactivate:'+id,'activate:captures']);
    f.ctx.select(id);
    assert.deepEqual(f.events,['deactivate:'+id,'activate:captures','deactivate:captures','activate:'+id]);
    assert.equal(f.events.some(event=>event.startsWith('close:')),false);
  }
});
test('selecting Computer creates one discoverable captures sibling without selecting it',()=>{
  const f=closingPanel({active:''}),source=fs.readFileSync(panelPath,'utf8');
  let serial=0;f.ctx.crypto={randomUUID:()=>String(++serial)};f.ctx.views={append(){}};
  f.ctx.document.createElement=()=>({hidden:true,setAttribute(){}});
  vm.runInContext(source.slice(source.indexOf('    function make('),source.indexOf('    function open(')),f.ctx);
  const computer=f.ctx.make('computer');computer.loaded=true;computer.activate=()=>f.events.push('activate:computer');
  f.ctx.select(computer.id);
  assert.equal(f.ctx.active,'computer');assert.equal(f.tabs.get('captures')?.kind,'captures');
  assert.equal(f.tabs.get('captures').element.hidden,true);
  const sibling=f.tabs.get('captures');f.ctx.select(computer.id);
  assert.equal(f.tabs.get('captures'),sibling);assert.deepEqual(f.events,['activate:computer']);
  f.tabs.delete('captures');f.ctx.select(computer.id);
  assert.equal(f.tabs.has('captures'),false,'Reselecting the active tab must not create an undrawn sibling');
});

function capturePanel(){
  const source=fs.readFileSync(panelPath,'utf8');
  const start=source.indexOf('    function mountCaptures(');
  assert.ok(start>=0,'The workspace owns a saved-captures lifecycle');
  const end=source.indexOf('\n    function ',start+1);
  const nodes=new Map(),requests=[],timers=new Map(),listeners=new Map(),opened=[];let serial=0,disposed=false;
  const video={pauses:0,pause(){this.pauses++;}};
  function node(selector){
    if(!nodes.has(selector))nodes.set(selector,{html:'',writes:0,get innerHTML(){return this.html;},set innerHTML(value){this.html=value;this.writes++;},textContent:'',hidden:false,disabled:false,dataset:{},isConnected:true,
      querySelector:node,querySelectorAll:kind=>kind==='video'?[video]:[],
      addEventListener(name,fn){this[name]=fn;},setAttribute(){}});
    return nodes.get(selector);
  }
  const tab={id:'captures',kind:'captures',element:node('tab')};
  const context={run:{id:'session'},esc:value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    good:t=>!disposed&&t===tab,openFile:file=>opened.push(file),size:n=>n+' B',
    api:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
    document:{hidden:false,addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)},
    setTimeout:(fn,delay)=>{timers.set(++serial,{fn,delay});return serial;},clearTimeout:id=>timers.delete(id)};
  vm.createContext(context);vm.runInContext(source.slice(start,end<0?undefined:end),context);context.mountCaptures(tab);
  return {tab,node,requests,timers,listeners,video,opened,
    async poll(){const [id,timer]=timers.entries().next().value;timers.delete(id);timer.fn();await new Promise(setImmediate);},
    dispose(){disposed=true;tab.dispose();}};
}
const captured=(name='proof.png',kind='image')=>({name,kind,path:'moyai-captures/'+name,archive_path:'capture:'+name,
  inline_url:'/captures/'+name,url:'/captures/'+name+'?download=true',size:12});
const settleCaptures=()=>new Promise(setImmediate);

test('captures read saved media without a live computer and discover newly completed captures',async()=>{
  const f=capturePanel();f.tab.activate();
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].path,'/api/runs/session/files');assert.equal(f.requests[0].options,undefined);
  f.requests[0].resolve({files:[]});await settleCaptures();
  assert.match(f.node('[data-captures]').innerHTML,/No saved captures yet/);
  assert.equal([...f.timers.values()][0].delay,3000);
  await f.poll();
  f.requests[1].resolve({files:[captured(),captured('flow.webm','video'),{...captured('unrelated.png'),archive_path:'new-files/unrelated.png'}]});
  await settleCaptures();
  const gallery=f.node('[data-captures]').innerHTML;
  assert.match(gallery,/proof\.png/);assert.match(gallery,/<video[^>]+flow\.webm/);assert.match(gallery,/download="proof\.png"/);
  assert.doesNotMatch(gallery,/unrelated\.png/);
  const writes=f.node('[data-captures]').writes;await f.poll();
  f.requests[2].resolve({files:[captured(),captured('flow.webm','video')]});await settleCaptures();
  assert.equal(f.node('[data-captures]').writes,writes,'Unchanged catalog refresh must not restart video playback');
  assert.ok(f.requests.every(request=>request.path.endsWith('/files')&&!request.options));
  f.dispose();
});
test('capture previews open the saved file while downloads and modified clicks stay native',async()=>{
  const f=capturePanel(),file=captured();f.tab.activate();f.requests[0].resolve({files:[file]});await settleCaptures();
  function click(extra={},download=false,href=file.inline_url){
    const link={hasAttribute:name=>name==='download'&&download,getAttribute:()=>href};
    const event={target:{closest:()=>link},preventDefault(){this.prevented=true;},...extra};
    f.node('[data-captures]').onclick(event);return event;
  }
  assert.equal(click().prevented,true);assert.deepEqual(f.opened,[file]);
  for(const modifier of ['metaKey','ctrlKey','shiftKey','altKey'])assert.equal(click({[modifier]:true}).prevented,undefined);
  assert.equal(click({},true,file.url).prevented,undefined);assert.equal(click({},false,'/not-a-capture').prevented,undefined);
  assert.deepEqual(f.opened,[file]);f.dispose();
});

test('capture refresh errors preserve saved media and retry successfully',async()=>{
  const f=capturePanel();f.tab.activate();f.requests[0].resolve({files:[captured()]});await settleCaptures();
  const gallery=f.node('[data-captures]').innerHTML;
  await f.poll();f.requests[1].reject(Object.assign(Error('Temporarily unavailable'),{status:503}));await settleCaptures();
  assert.equal(f.node('[data-captures]').innerHTML,gallery);
  assert.match(f.node('[data-status]').textContent,/Temporarily unavailable/);assert.equal(f.timers.size,1);
  await f.poll();f.requests[2].resolve({files:[captured(),captured('later.png')]});await settleCaptures();
  assert.match(f.node('[data-captures]').innerHTML,/later\.png/);assert.doesNotMatch(f.node('[data-status]').textContent,/Temporarily unavailable/);
  f.dispose();
});

test('missing saved files show an empty captures view that can refresh later',async()=>{
  const f=capturePanel();f.tab.activate();
  f.requests[0].reject(Object.assign(Error('No saved files are available yet.'),{status:404}));await settleCaptures();
  assert.match(f.node('[data-captures]').innerHTML,/No saved captures yet/);
  assert.doesNotMatch(f.node('[data-status]').textContent,/No saved files are available/);assert.equal(f.timers.size,1);
  await f.poll();f.requests[1].resolve({files:[captured()]});await settleCaptures();
  assert.match(f.node('[data-captures]').innerHTML,/proof\.png/);f.dispose();
});

test('hidden and disposed captures pause playback and reject late reads',async()=>{
  for(const dispose of [false,true]){
    const f=capturePanel();f.tab.activate();
    if(dispose)f.dispose();else f.tab.deactivate();
    assert.ok(f.video.pauses>0);assert.equal(f.timers.size,0);
    const hidden=f.node('[data-captures]').innerHTML;
    f.requests[0].resolve({files:[captured('stale.png')]});await settleCaptures();
    assert.equal(f.node('[data-captures]').innerHTML,hidden);assert.equal(f.timers.size,0);
    if(!dispose){
      f.tab.activate();assert.equal(f.requests.length,2);
      f.requests[1].resolve({files:[captured('current.png')]});await settleCaptures();
      assert.match(f.node('[data-captures]').innerHTML,/current\.png/);f.dispose();
    }
  }
});
test('returning to captures invalidates an earlier read before its delayed response arrives',async()=>{
  const f=capturePanel();f.tab.activate();f.tab.deactivate();f.tab.activate();
  assert.equal(f.requests.length,2);
  f.requests[1].resolve({files:[captured('current.png')]});await settleCaptures();
  const current=f.node('[data-captures]').innerHTML;
  f.requests[0].resolve({files:[captured('stale.png')]});await settleCaptures();
  assert.equal(f.node('[data-captures]').innerHTML,current);assert.match(current,/current\.png/);
  assert.equal(f.timers.size,1);f.dispose();
});

const native=require('../app/static/pull-request.js');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const details={number:145,title:'Native review',state:'open',draft:false,merged:false,base:'main',head:'abcdef0',head_ref:'feature',author:'alex',
  additions:2,deletions:1,changed_files:2,body:'Description',files:[{filename:'src/view.js',status:'modified',additions:2,deletions:1,patch:'@@ -4,2 +4,3 @@\n-old\n+new\n+added\n context'},
  {filename:'logo.png',status:'added',additions:0,deletions:0,patch:''}]};
const assertSame=(actual,expected,message)=>assert.ok(actual===expected,message||'Expected the value or retained node to stay identical');
// A small DOM double parses the controller's templates and runs the real keyed
// region owner. It tracks identity/disposal; React, layout and toggle events are
// verified separately against the production bundle in the browser suite.
function controllerDOM(){
  const document={activeElement:null},calls=[],disposed=[],roots=new Map();
  const encode=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
  const decode=value=>value.replace(/&(amp|lt|gt|quot|#39);/g,(_,name)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"}[name]));
  class Node{
    [require('node:util').inspect.custom](){return this.nodeType===3?this.text:`<${this.tagName.toLowerCase()} ${JSON.stringify(Object.fromEntries(this.attrs))}>`;}
    constructor(tag,text=''){this.tagName=tag?.toUpperCase();this.nodeType=tag?1:3;this.text=text;this.ownerDocument=document;this.parentNode=null;this.childNodes=[];this.attrs=new Map();this.style={};this.scrollTop=0;this.listeners=new Map();}
    get children(){return this.childNodes.filter(node=>node.nodeType===1);}
    get attributes(){return [...this.attrs].map(([name,value])=>({name,value}));}
    get dataset(){return Object.fromEntries([...this.attrs].filter(([name])=>name.startsWith('data-')).map(([name,value])=>[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),value]));}
    get isConnected(){return this===document.body||!!this.parentNode?.isConnected;}
    getAttribute(name){return this.attrs.get(name)??null;}
    hasAttribute(name){return this.attrs.has(name);}
    setAttribute(name,value){this.attrs.set(name,String(value));}
    removeAttribute(name){this.attrs.delete(name);}
    get className(){return this.getAttribute('class')||'';}
    set className(value){this.setAttribute('class',value);}
    get classList(){return {remove:(...names)=>{for(const name of names)this.classList.toggle(name,false);},toggle:(name,value)=>{const classes=new Set(this.className.split(/\s+/).filter(Boolean));if(value)classes.add(name);else classes.delete(name);this.className=[...classes].join(' ');}};}
    get hidden(){return this.hasAttribute('hidden');}set hidden(value){if(value)this.setAttribute('hidden','');else this.removeAttribute('hidden');}
    get open(){return this.hasAttribute('open');}set open(value){if(value)this.setAttribute('open','');else this.removeAttribute('open');}
    contains(node){return this===node||this.childNodes.some(child=>child.contains(node));}
    insertBefore(node,before){if(node===before)return;node.remove();const index=before?this.childNodes.indexOf(before):this.childNodes.length;assert.notEqual(index,-1);this.childNodes.splice(index,0,node);node.parentNode=this;}
    append(...nodes){nodes.forEach(node=>this.insertBefore(node,null));}
    remove(){if(!this.parentNode)return;if(this.contains(document.activeElement))document.activeElement=null;this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this),1);this.parentNode=null;}
    replaceChildren(...nodes){[...this.childNodes].forEach(node=>node.remove());this.append(...nodes);}
    get textContent(){return this.nodeType===3?this.text:this.childNodes.map(node=>node.textContent).join('');}
    set textContent(value){this.replaceChildren(new Node(null,String(value)));}
    get innerHTML(){return this.childNodes.map(node=>node.outerHTML).join('');}
    set innerHTML(value){this.replaceChildren(...parse(value));}
    get outerHTML(){return this.nodeType===3?encode(this.text):`<${this.tagName.toLowerCase()}${[...this.attrs].map(([key,value])=>` ${key}="${encode(value)}"`).join('')}>${this.innerHTML}</${this.tagName.toLowerCase()}>`;}
    matches(selector){if(selector.includes(','))return selector.split(',').some(part=>this.matches(part.trim()));if(selector.startsWith('.'))return this.className.split(/\s+/).includes(selector.slice(1));if(selector.startsWith('#'))return this.getAttribute('id')===selector.slice(1);const attr=/^\[([^=\]]+)(?:="([^"]*)")?\]$/.exec(selector);return attr?this.hasAttribute(attr[1])&&(attr[2]===undefined||this.getAttribute(attr[1])===attr[2]):this.tagName===selector.toUpperCase();}
    querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);}
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
    closest(selector){for(let node=this;node;node=node.parentNode)if(node.matches(selector))return node;return null;}
    addEventListener(type,listener){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(listener);}
    removeEventListener(type,listener){this.listeners.get(type)?.delete(listener);}
    click(){const event={target:this,defaultPrevented:false};for(let node=this;node;node=node.parentNode){event.currentTarget=node;node.onclick?.(event);for(const listener of node.listeners.get('click')||[])listener(event);}}
    focus(options){document.activeElement=this;this.focusOptions=options;}
    scrollIntoView(){}
  }
  function parse(html){
    const root=new Node('fragment'),stack=[root];
    for(const token of html.match(/<\/?[\w-]+(?:\s(?:[^>"']|"[^"]*"|'[^']*')*)?\s*\/?>|[^<]+/g)||[]){
      if(token.startsWith('</')){stack.pop();continue;}
      if(!token.startsWith('<')){stack.at(-1).append(new Node(null,decode(token)));continue;}
      const [,tag,attrs]=/^<([\w-]+)([\s\S]*?)\/?\s*>$/.exec(token),node=new Node(tag);
      for(const [,key,quoted,single,bare] of attrs.matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g))node.setAttribute(key,decode(quoted??single??bare??''));
      stack.at(-1).append(node);if(!['input','img','br','hr'].includes(tag)&&!token.endsWith('/>'))stack.push(node);
    }
    return [...root.childNodes];
  }
  document.body=new Node('body');document.querySelector=selector=>document.body.querySelector(selector);
  document.createElement=tag=>tag==='template'?{content:new Node('fragment'),set innerHTML(value){this.content.innerHTML=value;}}:new Node(tag);
  const context={document,MoyaiUI:{render(host,html){calls.push({host,html});for(const mounted of [...roots.keys()])if(host.contains(mounted)){disposed.push(mounted);roots.delete(mounted);}host.innerHTML=html;if(html)roots.set(host,Symbol('root'));}}};
  vm.createContext(context);vm.runInContext(fs.readFileSync('app/static/regions.js','utf8'),context);
  const element=new Node('main');document.body.append(element);
  return {context,element,document,calls,disposed,roots,node:selector=>element.querySelector(selector)};
}
function tabStrip(){
  const dom=controllerDOM(),source=fs.readFileSync(panelPath,'utf8'),ctx=dom.context;
  dom.element.innerHTML='<aside><div class="panel-tabs"></div><div class="panel-tools"><button data-add><span>Add</span></button><button data-expand><span>Expand</span></button><button data-hide><span>Hide</span></button></div><div class="panel-menu" hidden></div></aside><button id="workspace-panel-toggle"></button><button id="toggle-details"></button>';
  const panel=dom.node('aside'),tabs=new Map(['changes','files','activity'].map((id,index)=>[id,{id,uid:String(index),kind:id==='changes'?'pr':id,title:id,loaded:true,element:ctx.document.createElement('section')}]));
  Object.assign(ctx,{tabs,panel,active:'changes',visible:true,disposed:false,expanded:false,tabSignature:'',closingStatus:{},layout:dom.element,
    q:selector=>panel.querySelector(selector),esc:escape,ico:()=>'',glyph:{},MoyaiPullRequest:native,menu(value){panel.querySelector('.panel-menu').hidden=!value;},save(){},mount(){throw Error('Retained tab must not mount again');},
    card:ctx.document.createElement('aside'),outside(){},followPullRequest(){}});
  ctx.document.removeEventListener=()=>{};
  vm.runInContext(source.slice(source.indexOf('    function draw('),source.indexOf('    function hide(')),ctx);
  vm.runInContext(source.slice(source.indexOf('    function select('),source.indexOf('    function error(')),ctx);
  vm.runInContext(source.slice(source.indexOf("    const toolHost="),source.indexOf('    function outside(')),ctx);
  vm.runInContext(source.slice(source.indexOf('    function hide('),source.indexOf('    function menu(')),ctx);
  const dispose=source.slice(source.lastIndexOf('dispose(){')+'dispose(){'.length,source.lastIndexOf('}};'));
  vm.runInContext('function disposePanel(){'+dispose+'}',ctx);
  ctx.draw();dom.calls.length=0;return {...dom,ctx,tabs};
}
test('workspace tab switches retain controls and avoid duplicate strip rebuilds',()=>{
  const f=tabStrip(),unchanged=f.node('[data-tab="activity"]'),close=f.node('[data-close="activity"]'),old=f.node('[data-tab="changes"]');
  unchanged.focus();f.ctx.select('files');
  assertSame(f.node('[data-tab="activity"]'),unchanged);assertSame(f.node('[data-close="activity"]'),close);
  assertSame(f.node('[data-tab="changes"]'),old);assertSame(f.document.activeElement,unchanged);
  assertSame(old.getAttribute('aria-selected'),'false');assertSame(f.node('[data-tab="files"]').getAttribute('aria-selected'),'true');
  assertSame(f.calls.length,0,'Selection only changes controller-owned attributes');
  f.ctx.setVisible(false);assertSame(f.calls.length,0);assertSame(f.node('#workspace-panel-toggle').getAttribute('aria-expanded'),'false');
  f.tabs.get('activity').title='Latest activity';f.ctx.draw();
  assertSame(f.node('[data-tab="activity"]').getAttribute('title'),'Latest activity');assertSame(f.node('[data-tab="changes"]'),old);
});
test('retained tab clicks survive component click-slot changes without duplicate host handlers',()=>{
  const f=tabStrip(),strip=f.node('.panel-tabs'),first=f.node('[data-tab="changes"]');
  for(const id of ['activity','files','changes','activity']){
    const control=f.node(`[data-tab="${id}"]`);control.focus();control.onclick=()=>{};
    control.querySelector('span').click();assert.equal(f.ctx.active,id,'A nested label click reaches its persistent tab owner');
    assertSame(f.node('[data-tab="changes"]'),first);
  }
  assert.equal(strip.listeners.get('click')?.size,1,'Repeated draws retain one delegated listener');
  const close=f.node('[data-close="changes"]');close.innerHTML='<span>Close icon</span>';close.onclick=()=>{};close.querySelector('span').click();
  assert.equal(f.tabs.has('changes'),false);assert.equal(f.ctx.active,'activity');
  f.tabs.get('files').closing=true;f.ctx.draw();f.node('[data-close="files"]').click();assert.equal(f.tabs.has('files'),true,'Disabled close cannot remove a tab');
  f.ctx.disposed=true;f.node('[data-tab="files"]').click();assert.equal(f.ctx.active,'activity','Disposed owners reject later events');
});
test('panel toolbar actions retain independent click ownership and both delegates detach on disposal',()=>{
  const f=tabStrip(),tools=f.node('.panel-tools'),tabs=f.node('.panel-tabs');
  const click=selector=>{const button=f.node(selector);button.focus();button.onclick=()=>{};button.querySelector('span').click();};
  for(let i=0;i<3;i++){
    click('[data-add]');assert.equal(f.node('.panel-menu').hidden,false);click('[data-add]');assert.equal(f.node('.panel-menu').hidden,true);
    click('[data-expand]');assert.equal(f.ctx.expanded,true);click('[data-expand]');assert.equal(f.ctx.expanded,false);
    click('[data-hide]');assert.equal(f.ctx.visible,false);assertSame(f.document.activeElement,f.node('#workspace-panel-toggle'));f.ctx.select('activity');assert.equal(f.ctx.visible,true);
  }
  assert.equal(tools.listeners.get('click').size,1);assert.equal(tabs.listeners.get('click').size,1);
  f.ctx.disposePanel();assert.equal(tools.listeners.get('click').size,0);assert.equal(tabs.listeners.get('click').size,0);
  assert.equal(f.ctx.disposed,true);assert.equal(tools.isConnected,false);
});
test('workspace tab keyboard navigation and close states retain the surviving controls',async()=>{
  const f=tabStrip(),strip=f.node('.panel-tabs'),first=f.node('[data-tab="changes"]');
  const key=value=>strip.onkeydown({target:f.node('[aria-selected="true"]'),key:value,preventDefault(){}});
  key('End');assertSame(f.ctx.active,'activity');assertSame(f.document.activeElement,f.node('[data-tab="activity"]'));
  key('ArrowLeft');assertSame(f.ctx.active,'files');key('Home');assertSame(f.ctx.active,'changes');
  f.tabs.get('files').closing=true;f.ctx.draw();assertSame(f.node('[data-close="files"]').hasAttribute('disabled'),true);
  key('ArrowRight');assertSame(f.ctx.active,'activity');
  await f.ctx.remove('changes');assertSame(f.node('[data-tab="changes"]'),null);assertSame(first.isConnected,false);
  assertSame(f.ctx.active,'activity');
});
function nativePanel(){
  const dom=controllerDOM(),requests=[],markdownCalls=[],statuses=[];
  vm.runInContext(fs.readFileSync('app/static/pull-request.js','utf8'),dom.context);
  const lifecycle=dom.context.MoyaiPullRequest.mount({element:dom.element,url,escape,markdown:value=>{markdownCalls.push(value);return escape(value);},
    onStatus:data=>statuses.push(native.presentation(data).state),
    load:options=>new Promise((resolve,reject)=>requests.push({resolve,reject,options}))});
  return {...dom,requests,markdownCalls,statuses,...lifecycle};
}
async function initialPR(data=details){const f=nativePanel(),first=f.activate();f.requests[0].resolve(data);await first;f.calls.length=0;return f;}

test('PR activation may reuse recent data, while the Refresh button always requests fresh details',async()=>{
  const f=await initialPR();assert.equal(f.requests[0].options.refresh,false);
  const refresh=f.node('[data-refresh]').onclick();assert.equal(f.requests[1].options.refresh,true);
  f.requests[1].resolve(details);await refresh;
});
async function refreshPR(f,data=details){const read=f.node('[data-refresh]').onclick();f.requests.at(-1).resolve(data);await read;}
function toggleFile(f,index,open){const detail=f.element.querySelectorAll('[data-file]')[index];detail.open=open;detail.ontoggle();return detail;}
test('PR refresh revalidates but an identical payload preserves mounted headings and diff rows',async()=>{
  const f=await initialPR(),heading=f.node('[data-heading]').children[0],row=f.node('.pr-diff').children[0];
  f.deactivate();const reload=f.activate();assertSame(f.requests.length,2);assertSame(f.node('.pr-diff').children[0],row);
  f.requests[1].resolve(structuredClone(details));await reload;
  assertSame(f.node('[data-heading]').children[0],heading);assertSame(f.node('.pr-diff').children[0],row);assertSame(f.calls.length,0);
});
test('PR sections retain their mounted controls, diff identity and individual scroll positions',async()=>{
  const f=await initialPR(),content=f.node('[data-content]'),diff=f.node('.pr-diff');content.scrollTop=240;
  f.node('[data-section="description"]').onclick();const description=f.node('.pr-description');content.scrollTop=75;
  f.node('[data-section="changes"]').onclick();assertSame(content.scrollTop,240);assertSame(f.node('.pr-diff'),diff);
  assertSame(f.node('[data-pr-section="description"]').hidden,true);
  f.node('[data-section="description"]').onclick();assertSame(content.scrollTop,75);assertSame(f.node('.pr-description'),description);
  assert.deepEqual(f.markdownCalls,['Description']);f.calls.length=0;f.node('[data-section="description"]').onclick();assertSame(f.calls.length,0);
});
test('unopened files mount no diff rows and expanding one preserves every other file',async()=>{
  const files=[details.files[0],{...details.files[0],filename:'second.js',patch:'@@ -1 +1 @@\n-old two\n+new two'},{...details.files[0],filename:'third.js',patch:'+<script>third</script>'}];
  const f=await initialPR({...details,files}),first=f.element.querySelectorAll('[data-file]')[0],firstTable=first.querySelector('.pr-diff');
  assertSame(f.element.querySelectorAll('.pr-diff').length,1);assert.doesNotMatch(f.node('[data-content]').textContent,/new two|third<\/script>/);
  const second=toggleFile(f,1,true),secondTable=second.querySelector('.pr-diff');assert.match(secondTable.textContent,/new two/);
  assertSame(first.querySelector('.pr-diff'),firstTable);assertSame(f.element.querySelectorAll('.pr-diff').length,2);
  toggleFile(f,1,false);f.calls.length=0;toggleFile(f,1,true);assertSame(second.querySelector('.pr-diff'),secondTable);assertSame(f.calls.length,0);
  toggleFile(f,0,false);await refreshPR(f,{...details,files});assertSame(first.open,false,'Manual collapse survives fresh unchanged data');
});
test('PR refresh updates one file, defers changed closed bodies and disposes removed files',async()=>{
  const files=[details.files[0],{...details.files[0],filename:'second.js',patch:'+old second'}],f=await initialPR({...details,files});
  const first=f.element.querySelectorAll('[data-file]')[0],firstTable=first.querySelector('.pr-diff'),second=toggleFile(f,1,true),oldBody=second.querySelector('.pr-diff');
  toggleFile(f,1,false);await refreshPR(f,{...details,files:[files[0],{...files[1],patch:'+new second',additions:7}]});
  assertSame(first.querySelector('.pr-diff'),firstTable);assertSame(second.querySelector('.pr-diff'),null);assertSame(oldBody.isConnected,false);
  assert.match(second.querySelector('.pr-additions').textContent,/7/);toggleFile(f,1,true);assert.match(second.querySelector('.pr-diff').textContent,/new second/);
  const wrapper=second.parentNode;f.disposed.length=0;await refreshPR(f,{...details,files:[files[0]]});
  assertSame(second.isConnected,false);assert.ok(f.disposed.includes(wrapper),'Removing a file releases its disclosure root');assertSame(first.querySelector('.pr-diff'),firstTable);
});
test('native diffs show source line numbers, missing patches and truncation without interpreting code as HTML',async()=>{
  assert.deepEqual(native.diffRows(details.files[0].patch).map(r=>[r.old,r.next]),[['',''],[4,''],['',4],['',5],[5,6]]);
  const f=await initialPR({...details,files_truncated:true,files:[{...details.files[0],filename:'<script>.js',patch:'+<img onerror=x>',patch_truncated:true},details.files[1]]});
  assert.match(f.node('[data-content]').textContent,/<script>\.js/);assert.match(f.node('.pr-diff').textContent,/<img onerror=x>/);
  assert.equal(f.node('img'),null);assert.equal(f.node('script'),null);
  assert.match(f.node('[data-content]').textContent,/first 100 files/);assert.match(f.node('[data-content]').textContent,/diff is truncated/);
  toggleFile(f,1,true);assert.match(f.node('[data-content]').textContent,/did not provide a text diff/);
});
test('native PR refresh keeps prior data on temporary failure and retries successfully',async()=>{
  const f=await initialPR(),body=f.node('[data-content]').innerHTML,table=f.node('.pr-diff');
  const refresh=f.node('[data-refresh]').onclick();f.requests[1].reject(Object.assign(Error('Offline'),{status:502}));await refresh;
  assert.equal(f.node('[data-content]').innerHTML,body);assertSame(f.node('.pr-diff'),table);assert.match(f.node('[data-status]').textContent,/previous version/);
  assert.deepEqual(f.statuses,['open'],'Temporary failures preserve the last confirmed tab status');
  await refreshPR(f,{...details,title:'Recovered'});assert.match(f.node('[data-heading]').textContent,/Recovered/);
  assertSame(f.node('.pr-diff'),table);assert.equal(f.node('[data-status]').textContent,'');
});
test('every access-invalidating response clears visible and hidden PR sections and permits a fresh retry',async()=>{
  for(const status of [401,403,404,409])for(const selected of ['changes','description']){
    const f=await initialPR();f.node('[data-section="description"]').onclick();
    if(selected==='changes')f.node('[data-section="changes"]').onclick();
    const table=f.node('.pr-diff'),description=f.node('.pr-description');
    const denied=f.node('[data-refresh]').onclick();f.requests[1].reject(Object.assign(Error('Access removed'),{status}));await denied;
    assert.equal(f.node('[data-content]').textContent,'');assert.equal(f.node('[data-heading]').textContent,'');assert.equal(f.node('[data-state]').textContent,'Unavailable');
    assert.equal(table.isConnected,false);assert.equal(description.isConnected,false);assert.ok(f.disposed.length>0);
    assert.deepEqual(f.statuses,['open','unknown'],'Access denial also clears the tab status');
    await refreshPR(f);assert.match(f.node('[data-content]').textContent,/src\/view.js/);assert.equal(f.node('[data-status]').textContent,'');
    assert.deepEqual(f.statuses,['open','unknown','open'],'A fresh retry restores the confirmed status');
    f.node('[data-section="description"]').onclick();assert.match(f.node('.pr-description').textContent,/Description/);
  }
});
test('description refresh invalidates only changed Markdown and updates open diffs without reopening collapsed files',async()=>{
  const f=await initialPR();f.node('[data-section="description"]').onclick();const oldDescription=f.node('.pr-description');
  f.node('[data-section="changes"]').onclick();const detail=toggleFile(f,0,false);
  await refreshPR(f,{...details,title:'New title',body:'Updated description'});
  assert.equal(detail.open,false);assert.equal(oldDescription.isConnected,false);assert.deepEqual(f.markdownCalls,['Description']);
  f.node('[data-section="description"]').onclick();assert.deepEqual(f.markdownCalls,['Description','Updated description']);
  const description=f.node('.pr-description');f.node('[data-section="changes"]').onclick();toggleFile(f,0,true);
  const oldTable=f.node('.pr-diff');await refreshPR(f,{...details,body:'Updated description',files:[{...details.files[0],patch:'+Updated patch'},details.files[1]]});
  assert.equal(oldTable.isConnected,false);assert.match(f.node('.pr-diff').textContent,/Updated patch/);assertSame(f.node('.pr-description'),description);
  const mounted=[...f.roots.keys()];f.dispose();assert.equal(f.node('[data-content]').textContent,'');
  assert.ok(mounted.filter(host=>host!==f.element).every(host=>!f.roots.has(host)),'Disposal releases all nested content roots');
});
test('hidden, closed and reactivated PR tabs reject stale detail responses',async()=>{
  for(const ending of ['deactivate','dispose']){
    const f=nativePanel(),pending=f.activate();f[ending]();f.requests[0].resolve(details);await pending;
    assert.equal(f.node('[data-heading]').innerHTML,'');
    assert.deepEqual(f.statuses,[]);
  }
  const f=nativePanel(),old=f.activate();f.deactivate();const current=f.activate();
  f.requests[1].resolve({...details,title:'Current'});await current;
  f.requests[0].resolve({...details,title:'Stale'});await old;
  assert.match(f.node('[data-heading]').innerHTML,/Current/);assert.doesNotMatch(f.node('[data-heading]').innerHTML,/Stale/);
  assert.deepEqual(f.statuses,['open']);
});

test('detail refreshes publish closed, reopened, draft and merged status to the owning tab',async()=>{
  const f=nativePanel();
  const states=[{state:'open'},{state:'closed'},{state:'open'},{state:'open',draft:true},
    {state:'closed',draft:true,merged:true}];
  for(const [index,change] of states.entries()){
    const pending=index?f.node('[data-refresh]').onclick():f.activate();
    f.requests[index].resolve({...details,...change});await pending;
    assert.equal(f.node('[data-state]').className,'pr-state pr-state-'+f.statuses.at(-1));
  }
  assert.deepEqual(f.statuses,['open','closed','open','draft','merged']);
});

test('session sync updates each PR tab independently without undoing a newer detail read',()=>{
  const source=fs.readFileSync(panelPath,'utf8');
  const first={url,title:'First',state:'open'},second={url:url.replace('145','146'),title:'Second',state:'closed'};
  const tabs=new Map([first,second].map(pr=>[pr.url,{id:pr.url,kind:'pr',url:pr.url,prStatus:pr,prReceipt:pr}]));
  const ctx={disposed:false,run:{id:'session'},pullRequests:[],prSignature:'',prUrl,tabs,MoyaiPullRequest:native,
    renderPulls(){},syncToolboxVisibility(){},draw(){},save(){},pullsSection:{},document:{querySelector:()=>null},remove:id=>tabs.delete(id)};
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('    function syncPullRequests('),source.indexOf('    function followPullRequest(')),ctx);
  const sync=prs=>ctx.syncPullRequests({id:'session',pull_requests:prs});
  tabs.get(url).prStatus={state:'closed',merged:true};
  sync([{...first,title:'Renamed'},second]);
  assert.equal(native.presentation(tabs.get(url).prStatus).state,'merged');
  assert.equal(tabs.get(url).title,'Renamed');
  assert.equal(native.presentation(tabs.get(second.url).prStatus).state,'closed');
  sync([{...first,state:'merged'},{...second,state:'open'}]);
  assert.equal(native.presentation(tabs.get(url).prStatus).state,'merged');
  assert.equal(native.presentation(tabs.get(second.url).prStatus).state,'open');
  sync([first]);assert.equal(tabs.has(second.url),false);
});

function agentToolbox(){
  const source=fs.readFileSync(panelPath,'utf8');
  const host=()=>({hidden:false,writes:0,html:'',contains:()=>false,querySelector:()=>null,
    set innerHTML(value){this.html=value;this.writes++;},get innerHTML(){return this.html;}});
  const card=host(),pullsSection=host(),agentsSection=host(),agentTab=host(),classes=new Set(),toggle={hidden:true};
  const ctx={disposed:false,run:{id:'parent'},agents:[],agentSignature:'',pullRequests:[],card,pullsSection,agentsSection,
    tabs:new Map([['agents',{kind:'agents',element:agentTab}]]),
    document:{activeElement:null,querySelector:()=>toggle},ico:()=>'',
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    layout:{classList:{toggle:(key,value)=>value?classes.add(key):classes.delete(key)}}};
  vm.createContext(ctx);
  const app=fs.readFileSync('app/static/app.js','utf8');
  vm.runInContext(app.slice(app.indexOf('function sessionStatus('),app.indexOf('function sessionRepository(')),ctx);
  ctx.statusFor=ctx.sessionStatus;
  vm.runInContext(source.slice(source.indexOf('    function syncToolboxVisibility('),source.indexOf('    function renderPulls(')),ctx);
  return {ctx,card,pullsSection,agentsSection,agentTab,classes,toggle};
}
const childId='a'.repeat(32);
function team(children){return {id:'parent',agents:{groups:[{status:'completed',children}]}};}
test('agents alone show in the toolbox and tab with current status, safe links and escaped labels',()=>{
  const f=agentToolbox();
  f.ctx.syncAgents(team([{id:childId,agent_label:'<Explore> "design"',status:'running',session_url:'https://evil.test'},
    {id:'javascript:bad',status:'running'}]));
  assert.equal(f.card.hidden,false);assert.equal(f.pullsSection.hidden,true);assert.equal(f.toggle.hidden,false);
  assert.ok(f.classes.has('has-session-tools'));
  assert.match(f.agentsSection.html,/#run=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);
  assert.match(f.agentsSection.html,/&lt;Explore> &quot;design&quot;/);
  assert.match(f.agentsSection.html,/Working now/);assert.match(f.agentsSection.html,/0 of 1 ready/);
  assert.doesNotMatch(f.agentsSection.html,/evil.test|javascript:|<Explore>/);
  assert.equal(f.agentTab.html,f.agentsSection.html);
});
test('agent changes do not depend on PR changes and preserve unchanged rows',()=>{
  const f=agentToolbox();f.ctx.pullRequests=[{url}];
  for(const [status,label,ready] of [['queued','Queued',0],['running','Working now',0],['completed','Completed',1],['failed','Failed',0],['running','Working now',0],['idle','Ready',1],['waiting_credential','Needs access',0],['cancelled','Stopped',0],['new-status','Status unknown',0]]){
    const data=team([{id:childId,agent_label:'Scout',status}]);
    f.ctx.syncAgents(data);assert.match(f.agentsSection.html,new RegExp(label));
    assert.match(f.agentsSection.html,new RegExp(`${ready} of 1 ready`));
    const writes=f.agentsSection.writes;f.ctx.syncAgents(data);assert.equal(f.agentsSection.writes,writes);
    assert.equal(f.pullsSection.hidden,false);
  }
  f.ctx.syncAgents(team([]));assert.equal(f.agentsSection.hidden,true);assert.equal(f.card.hidden,false);
  f.ctx.pullRequests=[];f.ctx.syncToolboxVisibility();assert.equal(f.card.hidden,true);
  assert.match(f.agentTab.html,/Subagents assigned to this session/);
});
test('agent updates reject old sessions and disposal, deduplicate membership, and restore the agents tab',()=>{
  const f=agentToolbox(),child={id:childId,status:'completed'};
  const data=team([child]);data.agents.groups.push({children:[child]});f.ctx.syncAgents(data);
  assert.match(f.agentsSection.html,/1 of 1 ready/);
  const writes=f.agentsSection.writes;
  f.ctx.syncAgents({...team([]),id:'another-session'});f.ctx.syncAgents({id:'parent'});
  f.ctx.disposed=true;f.ctx.syncAgents(team([]));assert.equal(f.agentsSection.writes,writes);
  const saved=restore(JSON.stringify({visible:true,active:'agents',tabs:[{id:'agents',kind:'agents',title:'Subagents'}]}));
  assert.equal(saved.tabs[0].kind,'agents');assert.equal(saved.active,'agents');
});
test('terminal children still reconcile after the sidebar sees completion first',()=>{
  const script=fs.readFileSync('app/static/app.js','utf8'),polls=[],callbacks=[];
  const ctx={state:{authenticated:true,selected:'parent',chatRun:{agents:{groups:[{}]}},runs:[{id:'parent',children:[{status:'completed'}]}]},
    document:{hidden:false},setInterval:fn=>callbacks.push(fn),refreshChat:id=>{polls.push(id);return Promise.resolve();}};
  vm.createContext(ctx);
  const start=script.indexOf('setInterval(()=>{if(state.authenticated&&!document.hidden&&state.selected');
  vm.runInContext(script.slice(start,script.indexOf('\n',start)),ctx);
  callbacks[0]();assert.deepEqual(polls,['parent']);
  ctx.state.chatRun=null;ctx.state.runs[0].children=[];callbacks[0]();assert.equal(polls.length,1);
  ctx.document.hidden=true;ctx.state.runs[0].children=[{status:'running'}];callbacks[0]();assert.equal(polls.length,1);
});
