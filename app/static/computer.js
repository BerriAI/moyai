/* A private, session-scoped view of the browser running inside Modal. */
window.MoyaiComputer = {
  create({api, escape:esc}) {
    let dialog, runId, timer, leaseTimer, current, busy=false, version=0;
    const q=s=>dialog.querySelector(s);
    const own=()=>current?.controller && current.controller===current.actor;
    function close(){dialog?.close();}
    function render(data){
      current=data;
      q('[data-address]').textContent=data.url||'Sandbox browser';
      q('[data-status]').textContent=data.recording?'● Recording':data.available?'Live · refreshes every second':'Workspace browser';
      q('[data-status]').classList.toggle('recording',!!data.recording);
      q('[data-screen]').hidden=!data.frame;
      q('[data-empty]').hidden=!!data.frame;
      if(data.frame)q('[data-screen]').src='data:image/jpeg;base64,'+data.frame;
      q('[data-screen]').classList.toggle('controlled',!!own());
      q('[data-empty-text]').textContent=data.has_sandbox?'Open the browser to preview this workspace.':'This workspace is asleep. Send Moyai a message to start it again.';
      q('[data-open]').hidden=!data.has_sandbox||!!data.frame;
      q('[data-control]').textContent=own()?'Release control':data.controller?'Someone has control':'Take control';
      q('[data-control]').disabled=busy||!data.has_sandbox||!!(data.controller&&!own());
      q('[data-record]').textContent=data.recording?'■ Stop recording':'● Record flow';
      q('[data-interact]').hidden=!own();
      dialog.querySelectorAll('[data-interact] button,[data-interact] input,[data-open]').forEach(b=>b.disabled=busy);
      dialog.querySelectorAll('[data-action]').forEach(b=>b.disabled=busy||!own()||!data.available);
      q('[data-notice]').textContent=data.notice||(own()?'You have control. Click the preview or type below. Moyai’s browser actions wait until you release it.':'Watch Moyai work here. Take control to interact with the same browser.');
      const captures=data.captures||[],signature=JSON.stringify(captures);
      if(q('[data-captures]').dataset.signature!==signature){
        q('[data-captures]').dataset.signature=signature;
        q('[data-captures]').innerHTML=captures.length?captures.map(file=>`<article class="computer-capture">${file.kind==='image'?`<a href="${esc(file.inline_url)}" target="_blank" rel="noopener"><img src="${esc(file.inline_url)}" alt="${esc(file.name)}" loading="lazy"></a>`:`<video src="${esc(file.inline_url)}" controls preload="metadata"></video>`}<div><span title="${esc(file.name)}">${esc(file.name)}</span><a href="${esc(file.url)}" download="${esc(file.name)}" aria-label="Download ${esc(file.name)}">↓ Download</a></div></article>`).join(''):'<p class="computer-no-captures">Screenshots and recordings will appear here.</p>';
      }
    }
    async function poll(){
      clearTimeout(timer);if(!dialog?.open||document.hidden||busy)return;
      const v=version;
      try{const data=await api(`/api/runs/${runId}/computer`);if(v===version&&dialog.open)render(data);}
      catch(error){if(v===version)q('[data-notice]').textContent=error.message;}
      finally{if(v===version&&dialog.open)timer=setTimeout(poll,1000);}
    }
    async function command(action,args={}){
      if(busy||!dialog?.open)return false;
      busy=true;clearTimeout(timer);const v=version;
      if(current)render(current);
      q('[data-notice]').textContent=action==='open'?'Opening the sandbox browser. A saved workspace may need a one-time browser update…':'Working…';
      let succeeded=false;
      try{await api(`/api/runs/${runId}/computer`,{method:'POST',body:JSON.stringify({action,args})});succeeded=true;}
      catch(error){if(v===version)q('[data-notice]').textContent=error.message;}
      finally{if(v===version){busy=false;if(succeeded)await poll();else timer=setTimeout(poll,4000);}}
      return succeeded;
    }
    async function open(id){
      close();runId=id;version++;busy=false;current=null;
      if(!dialog){
        dialog=document.createElement('dialog');dialog.className='computer-dialog';dialog.setAttribute('aria-labelledby','computer-title');document.body.append(dialog);
        dialog.addEventListener('close',()=>{
          clearTimeout(timer);clearInterval(leaseTimer);version++;
          if(own())api(`/api/runs/${runId}/computer`,{method:'POST',body:JSON.stringify({action:'release'})}).catch(()=>{});
          dialog.querySelectorAll('video').forEach(video=>video.pause());current=null;
        });
        document.addEventListener('visibilitychange',()=>{if(!document.hidden&&dialog.open)poll();else clearTimeout(timer);});
      }
      dialog.innerHTML=`<header class="computer-heading"><div><h2 id="computer-title">Computer</h2><span data-status>Connecting…</span></div><div><button type="button" data-control disabled>Take control</button><button type="button" class="icon-button" data-close aria-label="Close computer">×</button></div></header>
        <div class="computer-address"><span aria-hidden="true">▧</span><span data-address>Sandbox browser</span><span class="computer-private">Private workspace</span></div>
        <div class="computer-stage"><img data-screen alt="Live sandbox browser. Take control to click and type." tabindex="0" hidden><div data-empty class="computer-empty"><span aria-hidden="true">▧</span><h3>Your browser in the cloud</h3><p data-empty-text>Connecting to the workspace…</p><button type="button" data-open hidden>Open browser</button></div></div>
        <div class="computer-toolbar"><div><button type="button" data-action="back" disabled>← Back</button><button type="button" data-action="screenshot" disabled>Screenshot</button><button type="button" data-action="record" data-record disabled>● Record flow</button></div><p data-notice role="status">Connecting…</p></div>
        <div data-interact class="computer-inputs" hidden><form data-url-form><label class="sr-only" for="computer-url">Page URL</label><input id="computer-url" type="url" placeholder="https://… or http://localhost:3000" required><button>Go</button></form><form data-type-form><label class="sr-only" for="computer-text">Type into the browser</label><input id="computer-text" type="text" autocomplete="off" placeholder="Type into the selected field…" required><button>Type</button><button type="button" data-enter>↵ Enter</button></form></div>
        <section class="computer-captures"><div class="computer-captures-heading"><h3>Saved captures</h3><p>Available after the workspace closes · Shared with session viewers</p></div><div data-captures></div><p class="computer-limits">Browser only · No audio · Up to 10 minutes or 25 MB per recording · 64 MB of captures per session</p></section>`;
      q('[data-close]').onclick=close;
      q('[data-control]').onclick=()=>command(own()?'release':'claim');
      q('[data-open]').onclick=async()=>{if(await command('claim'))await command('open');};
      q('[data-action="back"]').onclick=()=>command('back');
      q('[data-action="screenshot"]').onclick=()=>command('screenshot',{name:'screenshot'});
      q('[data-record]').onclick=()=>command(current?.recording?'record_stop':'record_start',{name:'flow'});
      q('[data-url-form]').onsubmit=async event=>{event.preventDefault();await command('open',{url:q('#computer-url').value});};
      q('[data-type-form]').onsubmit=async event=>{event.preventDefault();if(await command('type',{text:q('#computer-text').value}))q('#computer-text').value='';};
      q('[data-enter]').onclick=()=>command('key',{key:'Enter'});
      const screen=q('[data-screen]');
      screen.onclick=event=>{if(!own()||busy)return;const rect=screen.getBoundingClientRect(),scale=Math.min(rect.width/current.width,rect.height/current.height),left=rect.left+(rect.width-current.width*scale)/2,top=rect.top+(rect.height-current.height*scale)/2,x=(event.clientX-left)/scale,y=(event.clientY-top)/scale;if(x>=0&&x<current.width&&y>=0&&y<current.height)command('click',{x,y});};
      screen.onkeydown=event=>{
        if(!own()||busy)return;let key=event.key===' '? 'Space':event.key;
        if((event.ctrlKey||event.metaKey)&&key.toLowerCase()==='a')key='Control+a';
        if(event.shiftKey&&key==='Tab')key='Shift+Tab';
        if(['Enter','Tab','Shift+Tab','Escape','Backspace','Delete','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Control+a','Space'].includes(key)){event.preventDefault();event.stopPropagation();command('key',{key});}
      };
      screen.addEventListener('wheel',event=>{if(!own())return;event.preventDefault();if(!busy)command('scroll',{dy:Math.round(event.deltaY)});},{passive:false});
      dialog.showModal();await poll();
      leaseTimer=setInterval(()=>{if(dialog.open&&!document.hidden&&own()&&!busy)command('claim');},25000);
    }
    return {open,close};
  }
};
