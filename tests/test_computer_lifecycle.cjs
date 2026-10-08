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
function browser(api,options={}){
  const timers=new Map(),intervals=new Map();let next=0,now=0;
  const document={hidden:false,createElement:node,addEventListener(name,fn){this[name]=fn;}};
  const context={window:{},document,performance:{now:()=>now},setTimeout:(fn,delay)=>{timers.set(++next,{fn,at:now+delay});return next;},
    clearTimeout:id=>timers.delete(id),setInterval:fn=>{intervals.set(++next,fn);return next;},clearInterval:id=>intervals.delete(id)};
  vm.createContext(context);vm.runInContext(fs.readFileSync(process.env.COMPUTER_SOURCE||'app/static/computer.js','utf8'),context);
  const view=context.window.MoyaiComputer.create({api,escape:s=>s,...options});
  return {view,document,async renew(){for(const fn of intervals.values())fn();await tick();},async flush(delay=25){
    now+=delay;
    for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();}
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
  assert.equal(commands[0].args.frame,false,'Native input opts into acknowledgments without a screenshot');
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
  assert.equal(commands.length,1);finishInput();await tick();
  assert.equal(commands.length,2,'Accepted input drains immediately after acknowledgment');
  assert.deepEqual(commands[1].args.events,[{type:'text',text:'b'},{type:'key',key:'Enter'}]);
  type(input,'must not cross sessions');view.close();await tick();assert.equal(commands.length,2);
  finishInput();await tick();
  assert.equal(commands[2].action,'release');assert.match(commands[2].path,/\/one\/computer$/);
});

test('one poll stays live during input and newer frames survive metadata or legacy acknowledgments',async()=>{
  for(const legacy of [false,true]){
    let reads=0,finishPoll,finishInput;
    const state=(image,stamp)=>({...frame(image),tab:'',frame_at:stamp});
    const metadata=state('unused',0);delete metadata.frame;delete metadata.frame_at;
    const {view,document,flush}=browser(async(path,options)=>{
      if(!options)return ++reads===1?state('initial',1):new Promise(resolve=>finishPoll=resolve);
      if(JSON.parse(options.body).action==='input')return new Promise(resolve=>finishInput=resolve);
      return metadata;
    });
    const host=node();await view.open('one',host);
    const screen=host.child.querySelector('[data-screen]');
    type(host.child.querySelector('[data-keyboard]'),'first');await flush(20);await flush(100);
    assert.equal(reads,2,'Polling continues while the input acknowledgment is pending');
    document.visibilitychange();document.visibilitychange();await flush(100);
    assert.equal(reads,2,'Visibility notifications cannot start overlapping reads');
    finishPoll(state('newest',3));await tick();
    assert.equal(screen.src,'data:image/jpeg;base64,newest');
    finishInput(legacy?state('stale-input',2):metadata);await tick();
    assert.equal(screen.src,'data:image/jpeg;base64,newest');assert.equal(screen.hidden,false);
    await flush(100);finishPoll(state('stale-poll',2));await tick();
    assert.equal(screen.src,'data:image/jpeg;base64,newest');
    await flush(100);finishPoll({...metadata,available:false});await tick();
    assert.equal(screen.hidden,true,'An unavailable desktop cannot retain its previous image');
    view.close();await tick();
  }
});

test('continuous acknowledged input does not postpone the next frame poll',async()=>{
  let reads=0,finishPoll;
  const metadata={...frame(),tab:''};delete metadata.frame;
  const {view,flush}=browser(async(path,options)=>{
    if(options)return metadata;
    return ++reads===1?{...frame('initial'),tab:'',frame_at:1}:new Promise(resolve=>finishPoll=resolve);
  });
  const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
  for(let i=0;i<8;i++){type(input,'x');await flush(20);}
  assert.ok(reads>1,'Continuous input cannot starve the scheduled poll');
  finishPoll({...frame('updated'),tab:'',frame_at:2});await tick();
  assert.equal(host.child.querySelector('[data-screen]').src,'data:image/jpeg;base64,updated');
  assert.equal(host.child.querySelector('[data-screen]').hidden,false);
  view.close();await tick();
});

test('a failed refresh preserves control and queued input until their acknowledgments',async()=>{
  let reads=0;const commands=[],acks=[];
  const metadata={...frame(),tab:''};delete metadata.frame;
  const {view,flush}=browser(async(path,options)=>{
    if(!options){
      if(++reads===2)throw new Error('Computer is reconnecting');
      return {...frame('read-'+reads),tab:'',frame_at:reads};
    }
    const command=JSON.parse(options.body);
    if(command.action!=='input')return metadata;
    commands.push(command);return new Promise(resolve=>acks.push(resolve));
  });
  const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
  type(input,'first');await flush(20);type(input,'queued');await flush(100);
  assert.match(host.child.querySelector('[data-notice]').textContent,/reconnecting/);
  assert.equal(host.child.querySelector('[data-screen]').src,'data:image/jpeg;base64,read-1');
  assert.equal(input.readOnly,false);assert.equal(commands.length,1);
  acks.shift()(metadata);await tick();assert.equal(commands.length,2);
  assert.deepEqual(commands[1].args.events,[{type:'text',text:'queued'}]);
  acks.shift()(metadata);await tick();await flush(100);
  assert.equal(host.child.querySelector('[data-screen]').src,'data:image/jpeg;base64,read-3');
  assert.equal(input.readOnly,false);assert.equal(commands.length,2);
  assert.equal(host.child.querySelector('[data-control]').textContent,'Release control');view.close();await tick();
});

for(const shuttingDown of [false,true])test(`polling ${shuttingDown?'shutdown':'lease loss'} fences a late input acknowledgment`,async()=>{
  let reads=0,writes=0,finishInput;
  const metadata={...frame(),tab:''};delete metadata.frame;
  const {view,flush}=browser(async(path,options)=>{
    if(!options){
      const lost=++reads>1;
      return {...frame('screen',lost&&!shuttingDown?'other':'owner'),tab:'',frame_at:reads,shutting_down:lost&&shuttingDown};
    }
    if(JSON.parse(options.body).action==='input'){
      writes++;return new Promise(resolve=>finishInput=resolve);
    }
    return metadata;
  });
  const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
  type(input,'sent');await flush(20);type(input,'queued');await flush(100);
  assert.equal(input.readOnly,true);
  finishInput(metadata);await tick();type(input,'later');await flush(20);
  assert.equal(writes,1);assert.equal(input.readOnly,true);
  if(shuttingDown){
    assert.equal(host.child.querySelector('[data-control]').disabled,true);
    assert.equal(host.child.querySelector('[data-status]').textContent,'Shutting down…');
  }else assert.equal(host.child.querySelector('[data-control]').textContent,'Someone has control');
  view.close();await tick();
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
  const commands=[],inputArgs=[];let finish;
  const {view,flush}=browser(async(path,options)=>{
    if(!options)return frame();const command=JSON.parse(options.body);commands.push(command.action);
    if(command.action==='input')inputArgs.push(command.args);
    if(command.action==='input'&&commands.length===1)await new Promise(resolve=>finish=resolve);
    return frame(undefined,command.action==='release'?'':'owner');
  });
  const host=node();await view.open('one',host);const input=host.child.querySelector('[data-keyboard]');
  type(input,'sent');await flush();type(input,'unsent');
  const release=host.child.querySelector('[data-control]').onclick();
  type(input,'later');await flush();assert.deepEqual(commands,['input']);
  finish();await release;await flush();assert.deepEqual(commands,['input','input','release']);
  assert.ok(inputArgs.every(args=>args.frame===false),'Both ordinary and release-drained input omit frames');
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
test('desktop and PR views open saved captures separately without acquiring control',async()=>{
  for(const tab of ['',prA]){
    const commands=[],opened=[];
    const {view}=browser(async(path,options)=>{
      if(options)commands.push(JSON.parse(options.body));
      return {...(tab?prFrame(tab):frame(undefined,'')),captures:[{archive_path:'capture:proof.png',name:'proof.png',kind:'image',inline_url:'/proof.png',url:'/proof.png?download=true'}]};
    },{onCaptures:()=>opened.push('captures')});
    const host=node();await view.open('one',host,{tab});
    assert.match(host.child.innerHTML,/data-saved-captures/);
    assert.doesNotMatch(host.child.innerHTML,/data-captures|<section class="computer-captures"/);
    await host.child.querySelector('[data-saved-captures]').onclick();
    assert.deepEqual(opened,['captures']);assert.deepEqual(commands,[]);
    view.close();await tick();
  }
});
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

test('explicit wake stays scoped, blocks duplicate clicks and leaves control voluntary',async()=>{
  const requests=[];let finish,ready=false;
  const asleep={available:false,has_sandbox:false,can_wake:true,wake_supported:true,actor:'owner',tab:''};
  const {view,flush}=browser(async(path,options)=>{
    if(!options)return ready?frame(undefined,''):asleep;
    const command=JSON.parse(options.body);requests.push(command);
    if(command.action==='wake')return new Promise(resolve=>finish=resolve);
    return frame();
  });
  const host=node();await view.open('one',host);
  const wake=host.child.querySelector('[data-wake]');assert.equal(wake.hidden,false);
  const waking=wake.onclick();await tick();assert.equal(wake.disabled,true);
  await wake.onclick();assert.equal(requests.length,1);
  finish({...asleep,waking:true});await waking;
  assert.equal(wake.textContent,'Waking up…');assert.equal(wake.disabled,true);
  assert.equal(host.child.querySelector('[data-control]').disabled,true);
  ready=true;await flush(1000);
  assert.equal(wake.hidden,true);
  assert.equal(host.child.querySelector('[data-control]').textContent,'Take control');
  assert.deepEqual(requests,[{action:'wake',args:{},tab:''}]);view.close();
});

test('wake errors stay retryable without input-error copy and stale completion cannot switch sessions',async()=>{
  const asleep={available:false,has_sandbox:false,can_wake:true,wake_supported:true,actor:'owner',tab:''};
  let tries=0,finish;
  const {view}=browser(async(path,options)=>{
    if(!options)return path.includes('/next/')?frame('next',''):asleep;
    if(JSON.parse(options.body).action!=='wake')return frame();
    if(++tries===1)throw new Error('All workspaces are busy');
    return new Promise(resolve=>finish=resolve);
  });
  const host=node();await view.open('one',host);
  const wake=host.child.querySelector('[data-wake]');await wake.onclick();
  assert.equal(wake.disabled,false);
  assert.equal(host.child.querySelector('[data-notice]').textContent,'All workspaces are busy');
  const retry=wake.onclick();await tick();
  const next=node(),opening=view.open('next',next);finish({...asleep,waking:true});
  await retry;await opening;
  assert.equal(next.child.querySelector('[data-screen]').src,'data:image/jpeg;base64,next');
  assert.equal(next.child.querySelector('[data-wake]').hidden,true);view.close();
});

test('PR wake preserves tab scope and transient disconnect does not offer a false wake',async()=>{
  const calls=[];let state={available:false,has_sandbox:false,can_wake:true,wake_supported:true,tab:prA};
  const {view,flush}=browser(async(path,options)=>{
    if(options)calls.push(JSON.parse(options.body));return state;
  });
  const host=node();await view.open('one',host,{tab:prA});
  await host.child.querySelector('[data-wake]').onclick();assert.equal(calls[0].tab,prA);
  state={...state,can_wake:false,notice:'Computer is reconnecting'};await flush(1000);
  assert.equal(host.child.querySelector('[data-wake]').hidden,true);
  assert.equal(host.child.querySelector('[data-empty-text]').textContent,'Computer is reconnecting');view.close();
});

test('agent startup and blocked lifecycle states never invite another wake',async()=>{
  let state={available:false,has_sandbox:false,can_wake:false,starting:true,wake_notice:'Your workspace is starting. Its live computer will appear here.'};
  const {view,flush}=browser(async()=>state),host=node();await view.open('one',host);
  assert.equal(host.child.querySelector('[data-wake]').hidden,true);
  assert.equal(host.child.querySelector('[data-status]').textContent,'Starting workspace…');
  assert.equal(host.child.querySelector('[data-empty-text]').textContent,state.wake_notice);
  state={...state,starting:false,wake_notice:'This session is waiting for approval.'};await flush(1000);
  assert.equal(host.child.querySelector('[data-wake]').hidden,true);
  assert.equal(host.child.querySelector('[data-empty-text]').textContent,state.wake_notice);
  state={...frame(undefined,''),starting:true};await flush(1000);
  assert.equal(host.child.querySelector('[data-status]').textContent,'Live desktop');
  assert.equal(host.child.querySelector('[data-control]').disabled,false);view.close();
});

test('shutdown revokes controls and queued input while preserving the live frame',async()=>{
  for(const tab of ['',prA]){
    let state={...frame(),tab,surface:tab?'browser':'desktop',controller_tab:tab};
    const commands=[];
    const {view,flush,document,renew}=browser(async(path,options)=>{if(options)commands.push(JSON.parse(options.body));return state;});
    const host=node();await view.open('one',host,{tab});
    await renew();assert.equal(commands.pop().action,'claim');
    const input=host.child.querySelector('[data-keyboard]');type(input,'discard on cleanup');
    state={...state,shutting_down:true,wake_notice:'This workspace is shutting down.',wake_error:'Desktop startup failed.'};
    document.visibilitychange();await tick();
    assert.equal(host.child.querySelector('[data-control]').disabled,true);
    assert.equal(host.child.querySelector('[data-open]').hidden,true);
    assert.equal(input.readOnly,true);
    assert.equal(host.child.querySelector('[data-status]').textContent,'Shutting down…');
    assert.match(host.child.querySelector('[data-screen]').src,/example/);
    type(input,'blocked');await flush();assert.equal(commands.length,0);
    await renew();await host.child.querySelector('[data-control]').onclick();
    assert.equal(commands.length,0);
    view.close();await tick();
  }
});
