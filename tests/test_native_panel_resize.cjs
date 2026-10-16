// Execute the production preference/layout/gesture helpers. Actual UI proof belongs to the parent capture.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const ts=require('typescript');
const compiled=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../native/web/panel-resize-state.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const loaded=import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const close=(a,b)=>assert.ok(Math.abs(a-b)<0.000001,`${a} != ${b}`);
function storage(){const values=new Map();return {values,writes:0,getItem(key){return values.get(key)??null;},setItem(key,value){this.writes++;values.set(key,value);}};}

test('default 65:35 uses workspace width after excluding the sidebar and divider',async()=>{
  const {panelLayout,DEFAULT_CHAT_RATIO,PANEL_DIVIDER_WIDTH}=await loaded;
  const layout=panelLayout(1440-280,DEFAULT_CHAT_RATIO,1440);
  assert.equal(layout.split,true);close(layout.chat/(layout.chat+layout.panel),.65);
  close(layout.chat+layout.panel+PANEL_DIVIDER_WIDTH,1160);close(layout.chat,748.8);close(layout.panel,403.2);
});
test('minimum chat320/panel300 clamps are temporary geometry, never rewritten preferences',async()=>{
  const {panelLayout,readPanelRatio,writePanelRatio,panelRatioStorageKey}=await loaded,s=storage(),key=panelRatioStorageKey('existing','google:person');
  writePanelRatio(s,key,.82);const preferred=readPanelRatio(s,key);
  assert.equal(panelLayout(700,preferred,1400).panel,300);
  assert.equal(panelLayout(700,.01,1400).chat,320);
  close(panelLayout(2000,preferred,2200).chat/(2000-8),.82);
  assert.equal(readPanelRatio(s,key),.82);assert.equal(s.writes,1);
});
test('existing1000px breakpoint and undersized containers disable the splitter without overflow',async()=>{
  const {panelLayout}=await loaded;
  assert.equal(panelLayout(1000,.65,1000).split,false);assert.equal(panelLayout(627,.65,1440).split,false);
  const exact=panelLayout(628,.65,1440);assert.equal(exact.split,true);assert.equal(exact.chat,320);assert.equal(exact.panel,300);
  for(const width of [0,-1,NaN,Infinity])assert.equal(panelLayout(width,.65,1440).split,false);
});
test('canonical user/scope keys isolate preferences and survive a new session/reload',async()=>{
  const {panelRatioStorageKey,writePanelRatio,readPanelRatio}=await loaded,s=storage();
  const key=panelRatioStorageKey('existing','google:person@example.test');writePanelRatio(s,key,.73);
  assert.equal(readPanelRatio(s,panelRatioStorageKey('existing','google:person@example.test')),.73);
  assert.equal(readPanelRatio(s,panelRatioStorageKey('existing','google:other@example.test')),.65);
  assert.equal(readPanelRatio(s,panelRatioStorageKey('standalone','google:person@example.test')),.65);
  assert.notEqual(panelRatioStorageKey('a:b','c'),panelRatioStorageKey('a','b:c'));
  assert.equal(panelRatioStorageKey('existing',''),null);
});
test('corrupt, unsupported or blocked storage safely falls back without crashing',async()=>{
  const {decodePanelRatio,readPanelRatio,writePanelRatio}=await loaded;
  for(const raw of [null,'bad','[]','null','{"version":2,"chatRatio":0.7}','{"version":1,"chatRatio":"0.7"}','{"version":1,"chatRatio":0}','{"version":1,"chatRatio":1}','x'.repeat(201)])assert.equal(decodePanelRatio(raw),.65);
  const blocked={getItem(){throw Error('blocked');},setItem(){throw Error('quota');}};
  assert.equal(readPanelRatio(blocked,'key'),.65);assert.equal(writePanelRatio(blocked,'key',.7),false);
});
test('clicking an auto-clamped divider never persists the clamp',async()=>{
  const {PanelResizeDrag,panelLayout}=await loaded,layout=panelLayout(700,.9,1440),left=280,x=left+layout.chat+4+2;
  const drag=new PanelResizeDrag(1,.9,700,x,left,1440);
  drag.move(1,x,left,700);assert.equal(drag.finish(1),null);
});
test('dragging preserves the pointer grab offset and commits the final bounded ratio once',async()=>{
  const {PanelResizeDrag,panelLayout}=await loaded,layout=panelLayout(1160,.65,1440),left=280,x=left+layout.chat+6;
  const drag=new PanelResizeDrag(3,.65,1160,x,left,1440);
  close(drag.move(3,x-100,left,1160),(layout.chat-100)/layout.available);
  close(drag.finish(3),(layout.chat-100)/layout.available);assert.equal(drag.finish(3),null);assert.equal(drag.move(3,x,left,1160),null);
});
test('dragged sizes honor both pane minima and returning to the starting position is a no-op',async()=>{
  const {PanelResizeDrag,panelLayout}=await loaded,layout=panelLayout(1160,.65,1440),x=layout.chat+4;
  const left=new PanelResizeDrag(1,.65,1160,x,0,1440);close(left.move(1,-1000,0,1160),320/1152);
  const right=new PanelResizeDrag(1,.65,1160,x,0,1440);close(right.move(1,3000,0,1160),(1152-300)/1152);
  const back=new PanelResizeDrag(1,.65,1160,x,0,1440);back.move(1,x-50,0,1160);back.move(1,x,0,1160);assert.equal(back.finish(1),null);
});
test('secondary pointers cannot alter or finish the active drag',async()=>{
  const {PanelResizeDrag}=await loaded,drag=new PanelResizeDrag(1,.65,1160,752.8,0,1440);
  assert.equal(drag.move(2,100,0,1160),null);assert.equal(drag.finish(2),null);
  assert.notEqual(drag.move(1,650,0,1160),null);assert.notEqual(drag.finish(1),null);
});
test('cancel, hide/unmount cleanup and viewport resizing cannot commit late pointer input',async()=>{
  const {PanelResizeDrag}=await loaded;
  const cancelled=new PanelResizeDrag(1,.65,1160,752.8,0,1440);cancelled.move(1,600,0,1160);cancelled.cancel();assert.equal(cancelled.finish(1),null);
  const resized=new PanelResizeDrag(1,.65,1160,752.8,0,1440);assert.equal(resized.move(1,600,0,1200),null);assert.equal(resized.finish(1),null);
});
test('keyboard arrows use effective geometry, shift uses larger steps, and Home/End/Enter remain explicit',async()=>{
  const {panelKeyboardRatio,panelLayout}=await loaded,layout=panelLayout(1160,.65,1440);
  close(panelKeyboardRatio('ArrowLeft',.65,1160,1440),(layout.chat-10)/1152);
  close(panelKeyboardRatio('ArrowRight',.65,1160,1440,true),(layout.chat+50)/1152);
  close(panelKeyboardRatio('Home',.65,1160,1440),320/1152);close(panelKeyboardRatio('End',.65,1160,1440),852/1152);
  assert.equal(panelKeyboardRatio('Enter',.8,1160,1440),.65);
  assert.equal(panelKeyboardRatio('ArrowRight',.9,700,1440),null,'A blocked arrow must not replace a wider preferred ratio');
  assert.notEqual(panelKeyboardRatio('ArrowLeft',.9,700,1440),null,'Moving away from a clamp works immediately');
  assert.equal(panelKeyboardRatio('ArrowRight',.65,900,900),null);
});
