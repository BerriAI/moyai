const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

function element(dataset={},parent=null){
  const classes=new Set(),attributes={};
  return {dataset,parent,classes,attributes,
    classList:{add:value=>classes.add(value),remove:value=>classes.delete(value)},
    setAttribute:(name,value)=>attributes[name]=value,removeAttribute:name=>delete attributes[name],
    contains(node){for(;node;node=node.parent)if(node===this)return true;return false;},
    closest(selector){const key=selector==='[data-drag-session]'?'dragSession':'dropFolder';return key in dataset?this:parent?.closest(selector)||null;}
  };
}
function harness(fail=false){
  const list=element(),handlers={},requests=[],errors=[],messages=[];
  list.addEventListener=(type,fn)=>handlers[type]=fn;
  const source=element({dragSession:'session'},list),target=element({dropFolder:'destination'},list);
  const state={runs:[{id:'session',folder_id:'original'}],folders:[{id:'destination',name:'Work'}],closedFolders:new Set(['destination']),folderStorageKey:'folders'};
  let renders=0,refreshes=0;
  const context={state,
    api:async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});if(fail)throw new Error('Move failed');},
    refreshRuns:async()=>{refreshes++;},renderSidebar:()=>{renders++;},
    toast:message=>messages.push(message),showError:error=>errors.push(error.message),localStorage:{setItem(){}}
  };
  vm.createContext(context);
  vm.runInContext(readFileSync('app/static/session-folders.js','utf8'),context);
  context.bindSessionFolderDragDrop(list);
  const transfer={types:[],setData(type){this.types.push(type);}};
  function event(target,dataTransfer=transfer){return {target,dataTransfer,preventDefault(){this.prevented=true;},stopPropagation(){}};}
  return {list,source,target,state,handlers,requests,errors,messages,event,transfer,get renders(){return renders;},get refreshes(){return refreshes;}};
}

test('a local session drop persists the move and opens a collapsed folder',async()=>{
  const h=harness();
  h.handlers.dragstart(h.event(h.source));
  assert.equal(h.state.draggedSessionId,'session');
  assert.equal(h.transfer.effectAllowed,'move');
  const over=h.event(h.target);
  h.handlers.dragover(over);
  assert.equal(over.prevented,true);
  assert.equal(h.target.classes.has('folder-drop-target'),true);
  await h.handlers.drop(h.event(h.target));
  assert.deepEqual(h.requests,[{path:'/api/runs/session/folder',body:{folder_id:'destination'}}]);
  assert.equal(h.refreshes,1);
  assert.equal(h.state.closedFolders.has('destination'),false);
  assert.equal(h.state.draggedSessionId,null);
  assert.equal(h.list.attributes['aria-busy'],undefined);
  assert.equal(h.target.classes.has('folder-drop-target'),false);
  assert.deepEqual(h.messages,['Moved to Work.']);
});

test('dropping into Recent unfiles the session',async()=>{
  const h=harness();h.target.dataset.dropFolder='';
  h.handlers.dragstart(h.event(h.source));
  await h.handlers.drop(h.event(h.target));
  assert.equal(h.requests[0].body.folder_id,null);
});

test('external drags, child agents and drops into the existing folder do not move sessions',async()=>{
  const h=harness();
  await h.handlers.drop(h.event(h.target,{types:['application/x-moyai-session']}));
  const child=h.event(element({},h.list));
  h.handlers.dragstart(child);
  assert.equal(child.prevented,true);
  h.target.dataset.dropFolder='original';
  h.handlers.dragstart(h.event(h.source));
  await h.handlers.drop(h.event(h.target));
  assert.equal(h.requests.length,0);
});

test('cancelling a drag removes its highlight without saving',()=>{
  const h=harness();h.handlers.dragstart(h.event(h.source));h.handlers.dragover(h.event(h.target));
  h.handlers.dragend();
  assert.equal(h.requests.length,0);
  assert.equal(h.state.draggedSessionId,null);
  assert.equal(h.target.classes.has('folder-drop-target'),false);
  assert.equal(h.list.classes.has('session-list-dragging'),false);
});

test('a failed move keeps the source membership and releases the drag controls',async()=>{
  const h=harness(true);h.handlers.dragstart(h.event(h.source));
  await h.handlers.drop(h.event(h.target));
  assert.equal(h.state.runs[0].folder_id,'original');
  assert.equal(h.state.closedFolders.has('destination'),true);
  assert.equal(h.state.folderMovePending,false);
  assert.equal(h.state.draggedSessionId,null);
  assert.equal(h.refreshes,0);
  assert.deepEqual(h.errors,['Move failed']);
});
