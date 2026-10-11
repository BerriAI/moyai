/* One private desktop and control lease, shared with the sandbox's agent. */
window.MoyaiComputer = {
  create({api, escape:esc, onCaptures}) {
    let dialog, runId, timer, leaseTimer, inputTimer, compositionTimer, current;
    let busy=false, inputInFlight=false, polling=null, changingControl=false, active=false, composing=false, closing=false, browserTab='', version=0, revision=0;
    let queue=[], pointer=null, inputError='', wakeError='', releasing=Promise.resolve(), pending=Promise.resolve();
    const q=selector=>dialog.querySelector(selector);
    const own=()=>!!(current?.controller && current.controller===current.actor && (current.controller_tab||'')===browserTab);
    const surface=()=>browserTab?'browser':'desktop';
    const legacy=()=>current?.available && current.surface!==surface();
    const canInput=()=>active && own() && current?.available && !current.shutting_down && current.surface===surface() && !inputError && !changingControl && !closing;
    const canClaimOnClick=()=>active && current?.has_sandbox && current.available && current.frame && !current.controller &&
      current.surface===surface() && !current.waking && !current.shutting_down && !inputError && !busy && !changingControl && !closing;
    // The proxy also bounds JSON after Python's ASCII escaping and whitespace.
    const inputSize=events=>JSON.stringify({events}).replace(/[^\x00-\x7f]/g,'xxxxxx').length+events.length*16;

    function discardInput(){
      queue=[];pointer=null;composing=false;
      clearTimeout(inputTimer);inputTimer=null;clearTimeout(compositionTimer);
      if(dialog)q('[data-keyboard]').value='';
    }
    function close(){
      const wasActive=active, id=runId, tab=browserTab;
      active=false;version++;revision++;discardInput();clearTimeout(timer);clearInterval(leaseTimer);
      if(wasActive)releasing=Promise.all([releasing,pending]).then(()=>api(`/api/runs/${id}/computer`,{
        method:'POST',body:JSON.stringify({action:'release',tab})
      })).catch(()=>{});
      dialog?.querySelectorAll('video').forEach(video=>video.pause());
      dialog?.remove();dialog=null;current=null;busy=false;inputInFlight=false;polling=null;changingControl=false;closing=false;inputError='';wakeError='';
    }
    function render(data){
      const controlled=own()&&current?.available&&!current.shutting_down&&current.surface===surface();
      if(browserTab&&(data.tab!==browserTab||(data.available&&data.surface!=='browser'))){
        data={available:false,has_sandbox:false,notice:'This workspace needs an updated browser. Open the PR in GitHub until the workspace browser restarts.'};
      }
      // Input acknowledgements carry control state; only frame reads carry pixels.
      // An older guest may still attach a frame to its delayed acknowledgement.
      if(data.available&&current?.available&&data.surface===current.surface&&(data.tab||'')===(current.tab||'')&&
          (!('frame' in data)||(data.frame&&data.frame_at&&current.frame_at&&data.frame_at<current.frame_at))){
        data={...data,frame:current.frame,frame_at:current.frame_at};
      }
      current=data;
      if(controlled&&(!own()||!data.available||data.shutting_down||data.surface!==surface())){
        revision++;discardInput();
        if(!changingControl&&!closing)inputError='Control changed. Check the desktop, then take or resume control.';
      }
      if(browserTab)q('[data-address]').textContent=data.url||browserTab;
      const starting=data.starting&&!data.has_sandbox;
      q('[data-status]').textContent=data.shutting_down?'Shutting down…':data.waking?'Waking up…':starting?'Starting workspace…':data.recording?'● Recording':data.available?(browserTab?'Live browser':'Live desktop'):(browserTab?'Workspace browser':'Workspace computer');
      q('[data-status]').classList.toggle('recording',!!data.recording);
      q('[data-screen]').hidden=!data.frame;
      q('[data-empty]').hidden=!!data.frame;
      if(data.frame&&q('[data-screen]').src!=='data:image/jpeg;base64,'+data.frame)q('[data-screen]').src='data:image/jpeg;base64,'+data.frame;
      q('[data-screen]').classList.toggle('controlled',canInput());
      q('[data-screen]').alt=`Live ${surface()}. ${canClaimOnClick()?'Click to take control and interact.':'Take control to click and type.'}`;
      q('[data-keyboard]').readOnly=!canInput();
      q('[data-keyboard]').tabIndex=canInput()?0:-1;
      q('[data-empty-text]').textContent=data.waking?'Waking your workspace. Your live computer will appear here.':data.has_sandbox?(browserTab?'Take control to open this pull request in the browser.':'Take control to open your desktop and browser.'):data.notice||data.wake_notice||(data.wake_supported===false?'This workspace is asleep. Enable durable sessions to wake its computer here.':'This workspace is asleep. Wake it up to see it live, then take control to use it.');
      q('[data-wake]').hidden=!!data.frame||!!data.has_sandbox||(!data.can_wake&&!data.waking);
      q('[data-wake]').textContent=data.waking?'Waking up…':'Wake up to see live';
      q('[data-wake]').disabled=busy||changingControl||closing||!!data.waking;
      q('[data-open]').hidden=!data.has_sandbox||!!data.frame||!!data.waking||!!data.shutting_down;
      q('[data-open]').disabled=busy||changingControl||closing||!!(data.controller&&!own());
      q('[data-control]').textContent=own()?(inputError?'Resume control':'Release control'):data.controller?'Someone has control':'Take control';
      q('[data-control]').disabled=changingControl||closing||!!data.waking||!!data.shutting_down||!data.has_sandbox||!!(data.controller&&!own())||!!legacy();
      q('[data-record]').textContent=data.recording?'■ Stop recording':'● Record flow';
      dialog.querySelectorAll('[data-action]').forEach(button=>button.disabled=busy||changingControl||!canInput()||!data.available);
      q('[data-notice]').textContent=legacy()?'Restart this workspace to enable desktop control. This browser is running an older version.':wakeError||data.wake_error||inputError||data.notice||((data.shutting_down||!data.has_sandbox)&&data.wake_notice)||(own()?
        `You have control. Click and type in the ${surface()}. Ctrl+Alt+Esc returns to these controls.`:
        data.controller?'Someone else has control. You can watch here.':canClaimOnClick()?`Click inside the ${surface()} to take control and interact.`:
        browserTab?'Take control to use this pull request’s browser.':'Watch Moyai work here. Take control to use the desktop.');
    }
    async function poll(){
      clearTimeout(timer);if(!active||document.hidden||(busy&&!inputInFlight)||changingControl||closing||polling===version)return;
      const v=version, r=revision, started=performance.now();polling=v;
      try{const data=await api(`/api/runs/${runId}/computer${browserTab?'?tab='+encodeURIComponent(browserTab):''}`);if(v===version&&r===revision&&active)render(data);}
      catch(error){if(v===version&&r===revision)q('[data-notice]').textContent=error.message;}
      finally{
        if(polling===v)polling=null;
        if(v===version&&active)timer=setTimeout(poll,own()?Math.max(0,50-(performance.now()-started)):1000);
      }
    }
    function scheduleInput(){
      if(!inputTimer&&queue.length&&canInput()&&!busy)inputTimer=setTimeout(flushInput,20);
    }
    function pauseInput(message){
      discardInput();inputError=message+` Input stopped. Check the ${surface()}, then resume control.`;
      if(current)render(current);
    }
    function enqueue(event){
      if(!canInput())return;
      const last=queue[queue.length-1];
      if(['text','paste'].includes(event.type)&&last?.type===event.type&&last.text.length+event.text.length<=1000)last.text+=event.text;
      else if(event.type==='pointer'&&event.phase==='move'&&last?.type==='pointer'&&last.phase==='move')queue[queue.length-1]=event;
      else queue.push(event);
      if(queue.length>500||inputSize(queue)>64000){pauseInput('The connection could not keep up with your input.');return;}
      scheduleInput();
    }
    function text(value,kind='text'){
      if(!value||!canInput())return;
      if(value.length>10000){pauseInput('Paste up to 10,000 characters at a time.');return;}
      // Small text events let long Unicode pastes fit the proxy's byte budget.
      const characters=Array.from(value);
      for(let start=0;start<characters.length;start+=1000)enqueue({type:kind,text:characters.slice(start,start+1000).join('')});
    }
    function takeInput(){
      const events=[];let characters=0;
      while(queue.length&&events.length<100&&inputSize([...events,queue[0]])<=15000){
        const count=queue[0].text?.length||0;
        if(characters+count>10000)break;
        characters+=count;events.push(queue.shift());
      }
      if(!events.length)pauseInput('This input is too large to send.');
      return events;
    }
    function flushInput(){
      inputTimer=null;
      if(busy||!canInput()||!queue.length)return;
      const events=takeInput();
      if(events.length)send('input',{events,...(!browserTab?{frame:false}:{})});
    }
    function send(action,args={}){
      const v=version, id=runId, tab=browserTab, liveInput=action==='input'&&!tab;
      busy=true;inputInFlight=liveInput;
      if(!liveInput){revision++;clearTimeout(timer);}
      const r=revision;
      if(current)render(current);
      const operation=(async()=>{
        try{
          const data=await api(`/api/runs/${id}/computer`,{method:'POST',body:JSON.stringify({action,args,tab})});
          if(v!==version||(liveInput&&r!==revision)||!active)return false;
          if('available' in data)render(data);
          else if(current)render({...current,captures:data.captures||current.captures});
          return true;
        }catch(error){
          if(v===version&&active){if(action==='wake')wakeError=error.message;else pauseInput(error.message);}
          return false;
        }finally{
          if(v===version&&active){
            busy=false;inputInFlight=false;if(current)render(current);
            flushInput();
            if(!liveInput)timer=setTimeout(poll,own()?50:1000);
          }
        }
      })();
      pending=operation;
      return operation;
    }
    async function command(action,args={}){
      if(!active||changingControl||closing||current?.shutting_down||(busy&&action!=='release'))return false;
      const v=version;
      if(action==='wake'){wakeError='';return send('wake');}
      if(action==='claim'||action==='release'){
        changingControl=true;clearTimeout(inputTimer);inputTimer=null;
        if(action==='claim')discardInput();
        if(current)render(current);
        await pending;
        if(v!==version||!active||closing)return false;
        if(action==='release'){
          while(queue.length&&own()&&current.available&&!inputError){
            const events=takeInput();
            if(!events.length)break;
            await send('input',{events,...(!browserTab?{frame:false}:{})});
            if(v!==version||!active||closing)return false;
          }
          discardInput();
        }
        if(action==='claim'&&inputError&&own()&&!await send('release')){
          changingControl=false;if(current)render(current);return false;
        }
        if(v!==version||!active||closing)return false;
        if(action==='claim')inputError='';
      }
      if(action==='claim')q('[data-notice]').textContent=browserTab?'Opening the pull request browser…':'Opening your desktop…';
      let succeeded=await send(action,args);
      if(v!==version||!active||closing)return false;
      if(succeeded&&action==='claim'&&own()&&(!current.available||browserTab))succeeded=await send('open');
      if(v!==version||!active||closing)return false;
      changingControl=false;if(current)render(current);
      if(succeeded&&['claim','open'].includes(action)&&canInput())q('[data-keyboard]').focus({preventScroll:true});
      scheduleInput();
      return succeeded;
    }
    document.addEventListener('visibilitychange',()=>{
      if(!document.hidden&&active)poll();else clearTimeout(timer);
    });
    async function open(id,host,options={}){
      close();runId=id;const opening=++version;await releasing;if(opening!==version)return;
      busy=false;current=null;browserTab=options.tab||'';active=true;
      dialog=document.createElement('section');dialog.className='computer-view';dialog.setAttribute('aria-label','Sandbox computer');host.append(dialog);
      const control='<button type="button" data-control disabled>Take control</button>';
      const controls=browserTab?`<div><a class="computer-github-link" href="${esc(browserTab)}" target="_blank" rel="noopener noreferrer">Open in GitHub ↗</a>${control}</div>`:control;
      MoyaiUI.render(dialog, `<header class="computer-heading"><div><h2>${browserTab?'Pull request':'Computer'}</h2><span data-status>Connecting…</span></div>${controls}</header>
        ${browserTab?'<div class="computer-address"><span data-address>Sandbox browser</span></div>':''}
        <div class="computer-stage"><img data-screen alt="Live ${surface()}. Take control to click and type." draggable="false" hidden><textarea data-keyboard class="computer-keyboard" aria-label="${browserTab?'Browser':'Desktop'} keyboard" aria-describedby="computer-input-hint" autocomplete="off" autocapitalize="off" spellcheck="false" tabindex="-1" readonly></textarea><div data-empty class="computer-empty"><span aria-hidden="true">▧</span><h3>Your ${browserTab?'browser':'computer'} in the cloud</h3><p data-empty-text>Connecting to the workspace…</p><button type="button" data-wake hidden>Wake up to see live</button><button type="button" data-open hidden>Open ${surface()}</button></div></div>
        <div class="computer-toolbar"><div>${browserTab?'<button type="button" data-action="back" disabled>← Back</button>':'<button type="button" data-action="open" disabled>Open browser</button>'}<button type="button" data-action="screenshot" disabled>Screenshot</button><button type="button" data-action="record" data-record disabled>● Record flow</button></div><button type="button" data-saved-captures>Saved captures</button><p id="computer-input-hint" data-notice role="status">Connecting…</p></div>`);
      q('[data-saved-captures]').onclick=()=>onCaptures?.();
      q('[data-control]').onclick=()=>{commitText();return command(own()&&!inputError?'release':'claim');};
      q('[data-open]').onclick=()=>command('claim');
      q('[data-wake]').onclick=()=>command('wake');
      if(browserTab)q('[data-action="back"]').onclick=()=>command('back');
      else q('[data-action="open"]').onclick=()=>command('open');
      q('[data-action="screenshot"]').onclick=()=>command('screenshot',{name:'screenshot'});
      q('[data-record]').onclick=()=>command(current?.recording?'record_stop':'record_start',{name:'flow'});
      const keyboard=q('[data-keyboard]'),screen=q('[data-screen]');
      function commitText(){const value=keyboard.value;keyboard.value='';text(value);}
      keyboard.oninput=event=>{if(!composing&&!event.isComposing)commitText();};
      keyboard.oncompositionstart=()=>{composing=true;};
      keyboard.oncompositionend=()=>{
        composing=false;
        // Browsers differ on whether the final input precedes compositionend.
        compositionTimer=setTimeout(()=>{if(opening===version&&active)commitText();},0);
      };
      keyboard.onpaste=event=>{event.preventDefault();event.stopPropagation();commitText();text(event.clipboardData?.getData('text/plain')||'','paste');};
      keyboard.onkeydown=event=>{
        if(!canInput())return;
        event.stopPropagation();
        if(event.ctrlKey&&event.altKey&&event.key==='Escape'){
          event.preventDefault();q('[data-control]').focus();return;
        }
        if(composing||event.isComposing||event.key==='Process'||event.key==='Dead')return;
        const modifier=(event.ctrlKey||event.metaKey||event.altKey)&&!event.getModifierState?.('AltGraph');
        if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='v')return; // Native paste supplies clipboard text.
        const named=/^(Enter|Tab|Escape|Backspace|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|Insert|F([1-9]|1[0-2]))$/;
        if(!named.test(event.key)&&!(modifier&&event.key.length===1))return;
        if(modifier&&event.key.length===1&&!/^[a-zA-Z0-9+\-=/?.,\[\]]$/.test(event.key)){event.preventDefault();return;}
        event.preventDefault();commitText();
        const modifiers=[];
        if(event.ctrlKey||event.metaKey)modifiers.push('Control');
        if(event.altKey)modifiers.push('Alt');
        if(event.shiftKey)modifiers.push('Shift');
        const key=event.key===' '?'Space':event.key.length===1?event.key.toLowerCase():event.key;
        enqueue({type:'key',key:[...modifiers,key].join('+')});
      };
      function position(event,clamp=false){
        const rect=screen.getBoundingClientRect(),scale=Math.min(rect.width/current.width,rect.height/current.height);
        const x=(event.clientX-rect.left-(rect.width-current.width*scale)/2)/scale;
        const y=(event.clientY-rect.top-(rect.height-current.height*scale)/2)/scale;
        if(!Number.isFinite(x)||!Number.isFinite(y)||(!clamp&&(x<0||y<0||x>=current.width||y>=current.height)))return null;
        return {x:Math.round(Math.max(0,Math.min(current.width-1,x))),y:Math.round(Math.max(0,Math.min(current.height-1,y)))};
      }
      screen.onclick=async event=>{
        if(event.button!==0||!canClaimOnClick())return;
        const point=position(event);if(!point)return;
        event.preventDefault();
        const v=version,{width,height}=current;
        // Wait for the server's lease before forwarding the completed click.
        // Never replay it after a failed claim, view change or resized desktop.
        if(!await command('claim')||v!==version||!canInput()||current.width!==width||current.height!==height)return;
        enqueue({type:'pointer',phase:'down',...point,button:0});
        enqueue({type:'pointer',phase:'up',...point,button:0});
      };
      screen.onpointerdown=event=>{
        if(!canInput()||pointer||![0,1,2].includes(event.button))return;
        const point=position(event);if(!point)return;
        event.preventDefault();keyboard.focus({preventScroll:true});
        pointer={id:event.pointerId,button:event.button,...point};screen.setPointerCapture(event.pointerId);
        enqueue({type:'pointer',phase:'down',...point,button:event.button});
      };
      screen.onpointermove=event=>{
        if(!canInput())return;const point=position(event,!!pointer);if(!point)return;
        if(pointer)Object.assign(pointer,point);
        enqueue({type:'pointer',phase:'move',...point,button:pointer?.button||0});
      };
      screen.onpointerup=event=>{
        if(!pointer)return;const held=pointer;pointer=null;
        if(canInput())enqueue({type:'pointer',phase:'up',...(position(event,true)||{x:held.x,y:held.y}),button:held.button});
        screen.releasePointerCapture(event.pointerId);
      };
      screen.onpointercancel=screen.onlostpointercapture=()=>{
        if(pointer){const {x,y,button}=pointer;pointer=null;enqueue({type:'pointer',phase:'up',x,y,button});}
      };
      screen.oncontextmenu=screen.onauxclick=event=>{if(own())event.preventDefault();};
      screen.addEventListener('wheel',event=>{
        if(!canInput())return;event.preventDefault();
        const point=position(event);if(point)enqueue({type:'pointer',phase:'move',...point,button:0});
        const scale=event.deltaMode===1?16:event.deltaMode===2?current.height:1;
        enqueue({type:'scroll',dx:Math.round(event.deltaX*scale),dy:Math.round(event.deltaY*scale)});
      },{passive:false});
      await poll();
      if(!active||opening!==version)return;
      if(options.autoload&&current?.has_sandbox&&!legacy())await command('claim');
      if(!active||opening!==version)return;
      leaseTimer=setInterval(()=>{if(!document.hidden&&canInput()&&!busy&&!queue.length)send('claim');},25000);
    }
    function closeTab(id,tab){
      const v=version, selected=active&&id===runId&&tab===browserTab;
      if(selected){closing=true;revision++;discardInput();clearTimeout(timer);if(current)render(current);}
      const cleanup=Promise.all([releasing,pending]).then(()=>api(`/api/runs/${id}/computer`,{
        method:'POST',body:JSON.stringify({action:'close_tab',tab})
      })).finally(()=>{
        if(selected&&v===version&&active){closing=false;changingControl=false;if(current)render(current);timer=setTimeout(poll,own()?100:1000);}
      });
      releasing=cleanup.catch(()=>{});return cleanup;
    }
    return {open,close,closeTab};
  }
};
