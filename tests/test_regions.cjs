const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const source=readFileSync('app/static/regions.js','utf8');

// Template parsing and component mounting are doubles; these tests exercise the
// controller's DOM ownership and lifecycle. Actual shadcn behavior is browser-tested.
const element=(tag,attrs={},...children)=>({tag,attrs,children});
const container=(key,...children)=>element('div',{'data-region-key':key},...children);
const leaf=(key,label=key)=>element('article',{'data-region-key':key,'data-region-leaf':''},element('button',{'data-label':label}));
function harness(){
  const document={activeElement:null},roots=new Map(),calls=[],disposed=[];
  class Element{
    constructor(tag){this.tagName=tag.toUpperCase();this.nodeType=1;this.ownerDocument=document;this.parentNode=null;this.childNodes=[];this.attrs=new Map();this.style={};}
    get children(){return this.childNodes;}
    get attributes(){return [...this.attrs].map(([name,value])=>({name,value}));}
    get isConnected(){return this===document.body||!!this.parentNode?.isConnected;}
    getAttribute(name){return this.attrs.get(name)??null;}
    hasAttribute(name){return this.attrs.has(name);}
    setAttribute(name,value){this.attrs.set(name,String(value));}
    removeAttribute(name){this.attrs.delete(name);}
    contains(node){return this===node||this.children.some(child=>child.contains(node));}
    insertBefore(node,before){
      if(node===before)return;
      node.remove();
      const index=before?this.children.indexOf(before):this.children.length;
      assert.notEqual(index,-1,'insertBefore requires a direct child');
      this.childNodes.splice(index,0,node);node.parentNode=this;
    }
    remove(){
      if(!this.parentNode)return;
      if(this.contains(document.activeElement))document.activeElement=null;
      this.parentNode.childNodes.splice(this.parentNode.children.indexOf(this),1);this.parentNode=null;
    }
    replaceChildren(...nodes){for(const child of [...this.children])child.remove();for(const node of nodes)this.insertBefore(node,null);}
    focus(options){document.activeElement=this;this.focusOptions=options;}
    get description(){return element(this.tagName,Object.fromEntries(this.attrs),...this.children.map(child=>child.description));}
    get innerHTML(){return JSON.stringify(this.children.map(child=>child.description));}
    get outerHTML(){return JSON.stringify([this.description]);}
  }
  const build=description=>{
    const node=new Element(description.tag);
    for(const [name,value] of Object.entries(description.attrs))node.setAttribute(name,value);
    node.replaceChildren(...description.children.map(build));return node;
  };
  document.body=new Element('body');
  document.createElement=tag=>tag==='template'?{
    content:{children:[]},set innerHTML(value){this.content.children=JSON.parse(value).map(build);},
  }:new Element(tag);
  const MoyaiUI={render(host,markup){
    calls.push({host,markup,connected:host.isConnected});
    for(const mounted of [...roots.keys()])if(host.contains(mounted)){disposed.push(mounted);roots.delete(mounted);}
    host.replaceChildren(...(markup?JSON.parse(markup).map(build):[]));
    if(markup)roots.set(host,Symbol('component root'));
  }};
  const context={MoyaiUI};vm.runInNewContext(source,context);
  const host=new Element('main');document.body.insertBefore(host,null);
  return {host,document,roots,calls,disposed,element:tag=>new Element(tag),sync:(...nodes)=>context.MoyaiRegions.sync(host,JSON.stringify(nodes))};
}

test('a changed row mounts only that leaf and retains sibling controls, drafts and focus',()=>{
  const h=harness();h.sync(container('sessions',leaf('first'),leaf('second')));
  const [first,second]=h.host.children[0].children,button=second.children[0],root=h.roots.get(second);
  button.value='draft';button.focus();h.calls.length=0;
  h.sync(container('sessions',leaf('first','updated'),leaf('second')));
  assert.equal(h.host.children[0].children[0],first);
  assert.equal(h.host.children[0].children[1],second);
  assert.equal(second.children[0],button);assert.equal(button.value,'draft');
  assert.equal(h.document.activeElement,button);assert.equal(h.roots.get(second),root);
  assert.deepEqual(h.calls.map(call=>call.host),[first]);
  assert(h.calls.every(call=>call.connected),'new roots mount after their native hosts are attached');
});

test('appending a message preserves activity owned by its controller and existing message roots',()=>{
  const h=harness(),slot=element('div',{'data-region-key':'activity','data-region-preserve':''});
  h.sync(container('transcript',leaf('message'),slot));
  const transcript=h.host.children[0],message=transcript.children[0],activity=transcript.children[1];
  const content=h.element('details');content.open=true;activity.insertBefore(content,null);activity.setAttribute('data-controller-state','loaded');
  const root=h.roots.get(message);h.calls.length=0;
  h.sync(container('transcript',leaf('message'),slot,leaf('answer')));
  assert.equal(h.host.children[0],transcript);assert.equal(h.roots.get(message),root);
  assert.equal(transcript.children[1],activity);assert.equal(activity.children[0],content);assert.equal(content.open,true);
  assert.equal(activity.getAttribute('data-controller-state'),'loaded');
  assert.deepEqual(h.calls.map(call=>call.host),[transcript.children[2]]);
});

test('collapsing a branch disposes all hidden descendants once, including same-turn mounts',()=>{
  const h=harness();h.sync(container('branch',leaf('child-a'),leaf('child-b')));
  const branch=h.host.children[0],children=[...branch.children];
  assert.equal(h.roots.size,2);assert(h.calls.every(call=>call.connected));h.calls.length=0;
  h.sync(container('branch'));
  assert.equal(h.host.children[0],branch);assert.equal(branch.children.length,0);assert.equal(h.roots.size,0);
  assert.deepEqual(h.calls.map(call=>call.host),[branch]);assert.deepEqual(h.disposed,children);
  h.sync(container('branch',leaf('child-a')));
  assert.notEqual(branch.children[0],children[0]);assert.equal(h.roots.size,1);
});

test('removing a detached branch also disposes nested roots without an observer tick',()=>{
  const h=harness();h.host.remove();h.sync(container('branch',leaf('child')));
  assert.equal(h.roots.size,1);assert.equal(h.calls[0].connected,false);
  h.sync();assert.equal(h.roots.size,0);assert.equal(h.host.children.length,0);
});

test('reordering stable leaves retains component roots and restores moved focus without scrolling',()=>{
  const h=harness();h.sync(leaf('first'),leaf('second'));
  const [first,second]=h.host.children,button=second.children[0];button.focus();h.calls.length=0;
  h.sync(leaf('second'),leaf('first'));
  assert.deepEqual(h.host.children,[second,first]);assert.equal(h.document.activeElement,button);
  assert.equal(button.focusOptions.preventScroll,true);assert.equal(h.calls.length,0);assert.equal(h.roots.size,2);
});

test('another controller taking a connected node does not let its former parent steal or dispose it',()=>{
  const h=harness();h.sync(leaf('message'));
  const original=h.host.children[0],root=h.roots.get(original),other=h.element('aside');
  h.document.body.insertBefore(other,null);other.insertBefore(original,null);
  h.sync(leaf('message'));
  assert.equal(original.parentNode,other);assert.equal(h.roots.get(original),root);
  assert.notEqual(h.host.children[0],original);assert.equal(h.roots.size,2);
});

test('changing a key between native and component ownership disposes the old subtree',()=>{
  const h=harness();h.sync(leaf('switch'));
  const first=h.host.children[0];
  h.sync(element('article',{'data-region-key':'switch'},leaf('child')));
  const native=h.host.children[0];assert.notEqual(native,first);assert(!h.roots.has(first));assert(!h.roots.has(native));
  const child=native.children[0];assert.equal(h.roots.size,1);
  h.sync(leaf('switch'));assert.notEqual(h.host.children[0],native);assert.equal(h.roots.size,1);
  assert(h.disposed.includes(child));assert(!h.roots.has(child));
});

test('native attributes update without remounting contents and unkeyed loading regions are released',()=>{
  const h=harness();h.sync(element('p',{role:'status'}));const loading=h.host.children[0];
  h.sync(element('article',{'data-region-key':'','data-region-leaf':'',hidden:''},element('button')));
  const row=h.host.children[0],root=h.roots.get(row);assert.equal(row.tagName,'ARTICLE');assert(!h.roots.has(loading));
  h.sync(element('article',{'data-region-key':'','data-region-leaf':'',class:'selected'},element('button')));
  assert.equal(h.host.children[0],row);assert.equal(h.roots.get(row),root);
  assert.equal(row.hasAttribute('hidden'),false);assert.equal(row.getAttribute('class'),'selected');
});
