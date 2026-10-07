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

const prA='https://github.com/BerriAI/moyai/pull/145',prB='https://github.com/BerriAI/moyai/pull/135';
const prFrame=(tab,controller='')=>({...frame(tab,controller),surface:'browser',tab,controller,controller_tab:controller?tab:''});
test('PR pages use explicit identity for open, control, poll and delayed release',async()=>{
  const requests=[];let finishClaim;
  const {view}=browser(async(path,options)=>{
    requests.push({path,body:options?JSON.parse(options.body):null});
    if(!options)return prFrame(decodeURIComponent(path.split('?tab=')[1]));
    if(JSON.parse(options.body).action==='claim')await new Promise(resolve=>finishClaim=resolve);
    return prFrame(JSON.parse(options.body).tab,'owner');
  });
  const first=node(),second=node();const opening=view.open('one',first,{tab:prA,autoload:true});await tick();
  assert.equal(requests[1].body.tab,prA);assert.equal(requests[1].body.action,'claim');
  const replacement=view.open('one',second,{tab:prB});
  finishClaim();await opening;await replacement;
  assert.equal(requests.some(r=>r.body?.action==='open'),false,'Cancelled PR open cannot navigate the replacement');
  assert.deepEqual(requests.find(r=>r.body?.action==='release').body,{action:'release',tab:prA});
  assert.equal(second.child.querySelector('[data-address]').textContent,prB);
  view.close();await tick();
});

test('PR activation preserves an existing browser page and restored tabs do not seize control',async()=>{
  const commands=[];const {view}=browser(async(path,options)=>{
    if(!options)return prFrame(prA);
    commands.push(JSON.parse(options.body));return prFrame(prA,'owner');
  });
  await view.open('one',node(),{tab:prA});assert.equal(commands.length,0);
  await view.open('one',node(),{tab:prA,autoload:true});
  assert.deepEqual(commands.at(-1),{action:'open',args:{},tab:prA},'No URL forces navigation on a previously opened tab');
  view.close();await tick();
});

test('legacy or wrong-tab frames never render under a PR title or trigger navigation',async()=>{
  const commands=[],host=node();const {view}=browser(async(path,options)=>{
    if(options){commands.push(JSON.parse(options.body));return prFrame(prA,'owner');}
    return frame('https://private-agent-page.example');
  });
  await view.open('one',host,{tab:prA,autoload:true});
  assert.equal(host.child.querySelector('[data-screen]').hidden,true);
  assert.match(host.child.querySelector('[data-notice]').textContent,/updated browser/);
  assert.equal(commands.length,0);view.close();await tick();
});

test('named PR browser input uses the shared pointer, keyboard and paste batches with its tab identity',async()=>{
  const commands=[];
  const {view,flush}=browser(async(path,options)=>{
    if(options)commands.push(JSON.parse(options.body));
    else assert.equal(path,'/api/runs/one/computer?tab='+encodeURIComponent(prA));
    return prFrame(prA,'owner');
  });
  const host=node();await view.open('one',host,{tab:prA});
  const input=host.child.querySelector('[data-keyboard]'),screen=host.child.querySelector('[data-screen]');
  screen.emit('pointerdown',{clientX:20,clientY:30,button:0,pointerId:1});
  screen.emit('pointerup',{clientX:20,clientY:30,button:0,pointerId:1});
  input.emit('compositionstart');type(input,'日本語',{isComposing:true});input.emit('compositionend');await flush(0);
  input.emit('paste',{clipboardData:{getData:()=>'+test@example.com'}});key(input,'Enter');await flush();
  assert.deepEqual(commands,[{action:'input',tab:prA,args:{events:[
    {type:'pointer',phase:'down',x:40,y:60,button:0},{type:'pointer',phase:'up',x:40,y:60,button:0},
    {type:'text',text:'日本語'},{type:'paste',text:'+test@example.com'},{type:'key',key:'Enter'}
  ]}}]);
  view.close();await tick();assert.deepEqual(commands.at(-1),{action:'release',tab:prA});
});

test('the expected surface and exact controller tab both gate direct input',async()=>{
  for(const [tab,data] of [
    ['',{...frame(),controller_tab:prA}],['',{...frame(),surface:'browser'}],
    [prA,{...prFrame(prA,'owner'),controller_tab:prB}],
    [prA,{...prFrame(prA,'owner'),controller_tab:undefined}],
    [prA,{...prFrame(prA,'owner'),surface:'desktop'}]
  ]){
    const commands=[];
    const {view,flush}=browser(async(path,options)=>{if(options)commands.push(JSON.parse(options.body));return data;});
    const host=node();await view.open('one',host,{tab});type(host.child.querySelector('[data-keyboard]'),'blocked');await flush();
    assert.equal(commands.length,0);assert.equal(host.child.querySelector('[data-keyboard]').readOnly,true);
    if(tab&&data.surface==='desktop')assert.equal(host.child.querySelector('[data-screen]').hidden,true);
    view.close();await tick();
  }
});

test('closing a PR during claim waits for its acknowledgment and prevents a late open',async()=>{
  const commands=[];let finishClaim;
  const {view,flush}=browser(async(path,options)=>{
    if(!options)return prFrame(prA);
    const command=JSON.parse(options.body);commands.push(command);
    if(command.action==='claim')await new Promise(resolve=>finishClaim=resolve);
    if(command.action==='close_tab')throw new Error('Stop the recording before closing this tab.');
    return prFrame(prA,'owner');
  });
  const host=node(),opening=view.open('one',host,{tab:prA,autoload:true});await tick();
  const closing=view.closeTab('one',prA);await tick();assert.equal(commands.length,1);
  finishClaim();await opening;await assert.rejects(closing,/Stop the recording/);
  assert.deepEqual(commands.map(command=>command.action),['claim','close_tab']);
  assert.ok(commands.every(command=>command.tab===prA));assert.notEqual(host.child.removed,true);
  type(host.child.querySelector('[data-keyboard]'),'resume');await flush();
  assert.equal(commands.at(-1).action,'input','A rejected close retains usable controls');
  view.close();await tick();
});

test('PR cleanup waits for in-flight input, discards its queue, and targets only the closed page',async()=>{
  const commands=[];let finishInput,finishClose;
  const {view,flush}=browser(async(path,options)=>{
    if(!options)return prFrame(prA,'owner');
    const command=JSON.parse(options.body);commands.push(command);
    if(command.action==='input')await new Promise(resolve=>finishInput=resolve);
    if(command.action==='close_tab')await new Promise(resolve=>finishClose=resolve);
    return prFrame(prA,'owner');
  });
  const host=node();await view.open('one',host,{tab:prA});const input=host.child.querySelector('[data-keyboard]');
  type(input,'in flight');await flush();type(input,'queued');
  const closing=view.closeTab('one',prA);type(input,'later');await flush();assert.equal(commands.length,1);
  finishInput();await tick();assert.deepEqual(commands.map(command=>command.action),['input','close_tab']);
  assert.equal(commands[1].tab,prA);assert.notEqual(host.child.removed,true);
  finishClose();await closing;view.close();await tick();
});
