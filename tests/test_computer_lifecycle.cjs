const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function node(){
  const children=new Map(), listeners=new Map();
  return {dataset:{},value:'',classList:{toggle(){}},setAttribute(){},remove(){this.removed=true;},
    focus(){this.focused=true;},blur(){this.focused=false;},setPointerCapture(){},releasePointerCapture(){},
    getBoundingClientRect(){return {left:0,top:0,width:640,height:360};},
    addEventListener(name,fn){listeners.set(name,fn);},
    emit(name,values={}){const event={preventDefault(){this.prevented=true;},stopPropagation(){},...values};
      (this['on'+name]||listeners.get(name))?.(event);return event;},
    querySelector(s){if(!children.has(s))children.set(s,node());return children.get(s);},querySelectorAll(){return [];},append(el){this.child=el;}};
}
function browser(api){
  const timers=new Map();let next=0;
  const document={hidden:false,createElement:node,addEventListener(name,fn){this[name]=fn;}};
  const context={window:{},document,setTimeout:(fn,delay)=>{timers.set(++next,{fn,delay});return next;},
    clearTimeout:id=>timers.delete(id),setInterval:()=>1,clearInterval(){}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(process.env.COMPUTER_SOURCE||'app/static/computer.js','utf8'),context);
  const view=context.window.MoyaiComputer.create({api,escape:s=>s});
  return {view,document,async flush(delay=25){
    for(const [id,timer] of [...timers])if(timer.delay<=delay){timers.delete(id);timer.fn();}
    await tick();
  }};
}
const tick=()=>new Promise(setImmediate);
const frame=(url='https://example.com',controller='owner')=>({url,has_sandbox:true,available:true,surface:'desktop',
  frame:url,width:1280,height:720,captures:[],actor:'owner',controller});
const key=(input,value,modifiers={})=>input.emit('keydown',{key:value,...modifiers});
function type(input,value,options={}){input.value=value;input.emit('input',options);}

test('hiding while a claim is in flight releases after the claim completes',async()=>{
  const actions=[];let finishClaim;
  const {view}=browser(async(path,options)=>{
    if(!options)return frame(undefined,'');
    const action=JSON.parse(options.body).action;actions.push(action);
    if(action==='claim')await new Promise(resolve=>finishClaim=resolve);
    return frame();
  });
  const host=node();await view.open('one',host);
  const claim=host.child.querySelector('[data-control]').onclick();
  await tick();view.close();await tick();assert.deepEqual(actions,['claim']);
  finishClaim();await claim;await tick();
  assert.deepEqual(actions,['claim','release']);assert.equal(host.child.removed,true);
});

test('late frames cannot overwrite a replacement computer view',async()=>{
  let finishOld;
  const {view}=browser(async(path,options)=>{
    if(options)return frame();
    if(path.includes('/old/'))return new Promise(resolve=>finishOld=resolve);
    return frame('https://new.example');
  });
  const old=node(),current=node();const opening=view.open('old',old);await tick();
  await view.open('new',current);
  finishOld(frame('https://old.example'));await opening;
  assert.equal(current.child.querySelector('[data-screen]').src,'data:image/jpeg;base64,https://new.example');
  assert.equal(old.child.removed,true);view.close();
});

test('direct text, shortcuts, composition and paste reach the desktop in order',async()=>{
  const commands=[];
  const {view,flush}=browser(async(path,options)=>{if(options)commands.push(JSON.parse(options.body));return frame();});
  const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
  key(input,'l',{metaKey:true});type(input,'https://example.com');key(input,'Enter');
  input.emit('compositionstart');type(input,'日本',{isComposing:true});
  assert.equal(commands.length,0);
  input.emit('compositionend');await flush(0);
  input.emit('paste',{clipboardData:{getData:()=>'+demo@example.com\nsecond line'}});
  key(input,'Tab',{shiftKey:true});key(input,'Backspace');await flush();
  assert.deepEqual(commands.flatMap(c=>c.args.events),[
    {type:'key',key:'Control+l'},{type:'text',text:'https://example.com'},{type:'key',key:'Enter'},
    {type:'text',text:'日本'},{type:'paste',text:'+demo@example.com\nsecond line'},{type:'key',key:'Shift+Tab'},{type:'key',key:'Backspace'}
  ]);
  assert.equal(input.value,'');
  key(input,'Escape',{ctrlKey:true,altKey:true});
  assert.equal(host.child.querySelector('[data-control]').focused,true);view.close();
});

test('input waits behind an in-flight batch and close discards unsent input',async()=>{
  const commands=[];let finishInput;
  const {view,flush}=browser(async(path,options)=>{
    if(!options)return frame();const command=JSON.parse(options.body);commands.push({path,...command});
    if(command.action==='input')await new Promise(resolve=>finishInput=resolve);
    return frame();
  });
  const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
  type(input,'a');await flush();type(input,'b');key(input,'Enter');await flush();
  assert.equal(commands.length,1);finishInput();await tick();await flush();
  assert.deepEqual(commands[1].args.events,[{type:'text',text:'b'},{type:'key',key:'Enter'}]);
  type(input,'must not cross sessions');view.close();await tick();assert.equal(commands.length,2);
  finishInput();await tick();
  assert.equal(commands[2].action,'release');assert.match(commands[2].path,/\/one\/computer$/);
});

test('lease loss and uncertain failures discard queued input without replay',async()=>{
  for(const failure of [false,true]){
    const commands=[];let settle;
    const {view,flush}=browser(async(path,options)=>{
      if(!options)return frame();const command=JSON.parse(options.body);commands.push(command);
      if(command.action==='input')return new Promise((resolve,reject)=>{settle=()=>failure?reject(new Error('Connection lost')):resolve(frame(undefined,'other'));});
      return frame();
    });
    const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
    type(input,'sent');await flush();type(input,'unsent');settle();await tick();await flush();
    type(input,'later');await flush();assert.equal(commands.length,1);
    if(failure)assert.match(host.child.querySelector('[data-notice]').textContent,/Connection lost/);
    view.close();await tick();
  }
});

test('pointer buttons, dragging and wheel use scaled desktop coordinates',async()=>{
  const commands=[];
  const {view,flush}=browser(async(path,options)=>{if(options)commands.push(JSON.parse(options.body));return frame();});
  const host=node();await view.open('one',host);const screen=host.child.querySelector('[data-screen]');
  screen.emit('pointerdown',{clientX:20,clientY:30,button:2,pointerId:1});
  screen.emit('pointermove',{clientX:40,clientY:50,button:2,pointerId:1});
  screen.emit('pointerup',{clientX:50,clientY:60,button:2,pointerId:1});
  screen.emit('wheel',{clientX:50,clientY:60,deltaX:4,deltaY:24,deltaMode:0});await flush();
  assert.deepEqual(commands.flatMap(c=>c.args.events),[
    {type:'pointer',phase:'down',x:40,y:60,button:2},
    {type:'pointer',phase:'move',x:80,y:100,button:2},
    {type:'pointer',phase:'up',x:100,y:120,button:2},
    {type:'pointer',phase:'move',x:100,y:120,button:0},{type:'scroll',dx:4,dy:24}
  ]);view.close();
});

test('older daemons require a workspace restart and cannot receive desktop input',async()=>{
  const commands=[];const legacy={...frame(),surface:undefined};
  const {view,flush}=browser(async(path,options)=>{if(options)commands.push(JSON.parse(options.body));return legacy;});
  const host=node();await view.open('one',host);type(host.child.querySelector('[data-keyboard]'),'blocked');await flush();
  assert.equal(commands.length,0);assert.match(host.child.querySelector('[data-notice]').textContent,/restart/i);
  view.close();
});

test('taking control opens an unopened desktop and focuses native keyboard input',async()=>{
  const commands=[];
  const {view}=browser(async(path,options)=>{
    if(!options)return {...frame(undefined,''),available:false,surface:undefined,frame:''};
    const command=JSON.parse(options.body);commands.push(command.action);
    return {...frame(),available:command.action!=='claim'};
  });
  const host=node();await view.open('one',host);
  await host.child.querySelector('[data-open]').onclick();
  assert.deepEqual(commands,['claim','open']);
  assert.equal(host.child.querySelector('[data-keyboard]').focused,true);view.close();
});

test('Unicode paste is split into bounded batches without losing text',async()=>{
  const commands=[];
  const {view,flush}=browser(async(path,options)=>{if(options)commands.push(JSON.parse(options.body));return frame();});
  const host=node();await view.open('one',host);
  const value='日本語'.repeat(3000);
  host.child.querySelector('[data-keyboard]').emit('paste',{clipboardData:{getData:()=>value}});
  for(let i=0;i<10;i++)await flush();
  assert.equal(commands.flatMap(command=>command.args.events).map(event=>event.text).join(''),value);
  for(const command of commands){
    assert.ok(command.args.events.length<=100);
    assert.ok(JSON.stringify(command.args).replace(/[^\x00-\x7f]/g,'xxxxxx').length+command.args.events.length*16<=16000);
  }
  view.close();
});

test('release drains accepted input in order and permits no further typing',async()=>{
  const commands=[];let finish;
  const {view,flush}=browser(async(path,options)=>{
    if(!options)return frame();const command=JSON.parse(options.body);commands.push(command.action);
    if(command.action==='input'&&commands.length===1)await new Promise(resolve=>finish=resolve);
    return frame(undefined,command.action==='release'?'':'owner');
  });
  const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
  type(input,'sent');await flush();type(input,'unsent');
  const release=host.child.querySelector('[data-control]').onclick();
  type(input,'later');await flush();assert.deepEqual(commands,['input']);
  finish();await release;await flush();assert.deepEqual(commands,['input','input','release']);
  assert.equal(input.readOnly,true);view.close();
});

test('the pending input queue is bounded and overflow stops rather than replays',async()=>{
  const commands=[];let finish;
  const {view,flush}=browser(async(path,options)=>{
    if(!options)return frame();const command=JSON.parse(options.body);commands.push(command);
    if(command.action==='input')await new Promise(resolve=>finish=resolve);
    return frame();
  });
  const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
  type(input,'in flight');await flush();
  for(let i=0;i<501;i++)key(input,'ArrowLeft');
  assert.match(host.child.querySelector('[data-notice]').textContent,/could not keep up/);
  finish();await tick();await flush();assert.equal(commands.length,1);
  view.close();
});
