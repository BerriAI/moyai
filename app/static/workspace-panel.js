/* Session-scoped tabs keep tools beside the conversation without blocking it. */
(function(root,factory){
  const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.MoyaiPanel=value;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function restore(value){
    try{
      const data=JSON.parse(value);
      const tabs=(Array.isArray(data.tabs)?data.tabs:[]).filter(t=>t&&['computer','files','file','activity','chat'].includes(t.kind)&&typeof t.id==='string'&&t.id.length<1200&&(!t.chatId||/^[a-f0-9]{32}$/.test(t.chatId))).slice(0,16).map(t=>({...t,title:String(t.title||'Tab').slice(0,200),draft:String(t.draft||'').slice(0,16000)}));
      return {visible:!!data.visible,active:String(data.active||''),width:Math.max(30,Math.min(70,Number(data.width)||60)),tabs};
    }catch{return {visible:false,active:'',width:60,tabs:[]};}
  }
  function fileTree(files){
    const root={directories:new Map(),files:[],path:''};
    files.forEach((file,index)=>{
      const parts=file.path.split('/');let node=root;
      for(const name of parts.slice(0,-1)){
        if(!node.directories.has(name))node.directories.set(name,{name,path:node.path?node.path+'/'+name:name,directories:new Map(),files:[]});
        node=node.directories.get(name);
      }
      node.files.push({file,index});
    });
    return root;
  }
  function renderFileTree(files,{escape:esc,size,expanded=new Set(),search=false}){
    function render(node){
      const folders=[...node.directories.values()].sort((a,b)=>a.name.localeCompare(b.name));
      const leaves=[...node.files].sort((a,b)=>a.file.name.localeCompare(b.file.name));
      return folders.map(folder=>`<details class="panel-file-folder" data-folder="${esc(folder.path)}" ${search||expanded.has(folder.path)?'open':''}><summary title="${esc(folder.path)}"><span aria-hidden="true">▱</span> ${esc(folder.name)}</summary><div class="panel-file-children">${render(folder)}</div></details>`).join('')+leaves.map(({file:f,index})=>`<button type="button" class="panel-file-choice" data-file="${index}" title="${esc(f.path)}"><span aria-hidden="true">${f.kind==='video'?'▷':f.kind==='image'?'▧':'▤'}</span><span><strong>${esc(f.name)}</strong><small>${size(f.size)}</small></span><span aria-hidden="true">↗</span></button>`).join('');
    }
    return render(fileTree(files))||'<p class="panel-empty">No matching saved files.</p>';
  }
  function create({run,layout,api,computer,markdown,escape:esc,size,user,models,toast,onCreated}){
    const key='moyai-panel:'+user+':'+run.id;
    let initial;try{initial=restore(localStorage.getItem(key));}catch{initial=restore(null);}
    const tabs=new Map();let active='',visible=false,width=initial.width,disposed=false,expanded=false,sideChats=[],restoring=true;
    const activity=layout.querySelector('#session-details');
    const ico=(name,size=16)=>globalThis.MoyaiIcon?.(name,size)||'';const glyph={computer:'monitor',files:'file',file:'file',chat:'chat',activity:'list'};
    const panel=document.createElement('aside');panel.className='workspace-panel';panel.id='workspace-panel';panel.setAttribute('aria-label','Session workspace');panel.hidden=true;
    panel.innerHTML=`<div class="panel-resize" role="separator" aria-label="Resize workspace panel" aria-orientation="vertical" tabindex="0"></div><header class="panel-header"><div class="panel-tabs" role="tablist" aria-label="Workspace tabs"></div><div class="panel-tools"><button type="button" class="panel-icon" data-add aria-label="Add tab" title="Add tab" aria-expanded="false">${ico('plus',18)}</button><span class="panel-spacer"></span><button type="button" class="panel-icon" data-expand aria-label="Expand workspace panel" title="Expand">${ico('expand',17)}</button><button type="button" class="panel-icon" data-hide aria-label="Hide workspace panel" title="Hide panel">${ico('panel',18)}</button></div></header><div class="panel-menu" hidden><label><span aria-hidden="true">⌕</span><input type="search" placeholder="Search tabs…" aria-label="Search workspace tabs"></label><div data-menu-items></div></div><div class="panel-views"></div><div data-parking hidden></div>`;
    layout.append(panel);const q=s=>panel.querySelector(s),views=q('.panel-views'),parking=q('[data-parking]');
    if(activity){parking.append(activity);activity.hidden=false;}
    function save(){if(restoring)return;try{localStorage.setItem(key,JSON.stringify({visible,active,width,tabs:[...tabs.values()].map(t=>({id:t.id,kind:t.kind,title:t.title,path:t.path,chatId:t.chatId,draft:t.draft||'',clientId:t.clientId,submission:t.submission,model:t.model}))}));}catch{}}
    function resize(next){width=Math.max(30,Math.min(70,next));layout.style.setProperty('--panel-width',width+'%');layout.closest('.workspace')?.style.setProperty('--workspace-panel-width',width+'%');q('.panel-resize').setAttribute('aria-valuenow',String(Math.round(width)));save();}
    resize(width);q('.panel-resize').setAttribute('aria-valuemin','30');q('.panel-resize').setAttribute('aria-valuemax','70');
    q('.panel-resize').onpointerdown=event=>{event.preventDefault();const grip=event.currentTarget;grip.setPointerCapture(event.pointerId);grip.onpointermove=e=>{const rect=layout.getBoundingClientRect();resize((rect.right-e.clientX)/rect.width*100);};grip.onpointerup=grip.onpointercancel=()=>{grip.onpointermove=null;save();};};
    q('.panel-resize').onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();resize(width+(e.key==='ArrowLeft'?3:-3));}};
    function draw(){
      q('.panel-tabs').innerHTML=[...tabs.values()].map(t=>`<div class="panel-tab ${t.id===active?'is-active':''}"><button type="button" role="tab" id="tab-${t.uid}" aria-controls="view-${t.uid}" aria-selected="${t.id===active}" tabindex="${t.id===active?'0':'-1'}" data-tab="${esc(t.id)}" title="${esc(t.title)}"><span class="panel-tab-icon">${ico(glyph[t.kind],15)}</span><span>${esc(t.title)}</span></button><button type="button" data-close="${esc(t.id)}" aria-label="Close ${esc(t.title)} tab">${ico('x',13)}</button></div>`).join('');
      q('.panel-tabs').querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>select(b.dataset.tab));
      q('.panel-tabs').querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>remove(b.dataset.close));
      document.querySelector('#toggle-details')?.setAttribute('aria-expanded',String(visible&&tabs.get(active)?.kind==='activity'));
      const toggle=document.querySelector('#workspace-panel-toggle');if(toggle){toggle.setAttribute('aria-expanded',String(visible));toggle.setAttribute('aria-label',visible?'Hide workspace panel':'Show workspace panel');toggle.classList.toggle('is-active',visible);}
    }
    q('.panel-tabs').onkeydown=e=>{if(!e.target.matches('[role="tab"]'))return;const ids=[...tabs.keys()],idx=ids.indexOf(active);let next;if(e.key==='ArrowRight')next=ids[(idx+1)%ids.length];if(e.key==='ArrowLeft')next=ids[(idx+ids.length-1)%ids.length];if(e.key==='Home')next=ids[0];if(e.key==='End')next=ids.at(-1);if(next){e.preventDefault();select(next);q('[aria-selected="true"]')?.focus();}if(e.key==='Delete'){e.preventDefault();remove(active);}};
    function setVisible(value){
      visible=value;panel.hidden=!value;layout.classList.toggle('panel-open',value);layout.classList.toggle('panel-expanded',value&&expanded);menu(false);draw();save();
    }
    function hide(){tabs.get(active)?.deactivate?.();setVisible(false);document.querySelector('#workspace-panel-toggle')?.focus();}
    function menu(open){q('.panel-menu').hidden=!open;q('[data-add]').setAttribute('aria-expanded',String(open));if(open){q('.panel-menu input').value='';drawMenu();q('.panel-menu input').focus();}}
    function drawMenu(){
      const search=q('.panel-menu input').value.toLowerCase();
      const items=[...(run.mode==='modal'?[{kind:'computer',title:'Computer',detail:'Watch and use the sandbox desktop'}]:[]),{kind:'files',title:'Files',detail:'Open saved files, screenshots, and videos'},{kind:'chat',title:'Side chat',detail:'A separate conversation about this session'},{kind:'activity',title:'Activity',detail:'Tools, approvals, and session details'},...sideChats.map(c=>({kind:'chat',title:c.prompt,detail:'Saved side chat',chatId:c.id}))].filter(i=>i.title.toLowerCase().includes(search));
      q('[data-menu-items]').innerHTML=items.map((item,i)=>`<button type="button" data-item="${i}"><span class="panel-tab-icon">${ico(glyph[item.kind],16)}</span><span><strong>${esc(item.title)}</strong><small>${esc(item.detail)}</small></span></button>`).join('')||'<p class="panel-empty">No matching tabs.</p>';
      q('[data-menu-items]').querySelectorAll('button').forEach(b=>b.onclick=()=>{const item=items[Number(b.dataset.item)];open(item.kind,item.chatId?{chatId:item.chatId,title:item.title}:{});menu(false);});
    }
    q('.panel-menu input').oninput=drawMenu;q('[data-add]').onclick=()=>menu(q('.panel-menu').hidden);q('[data-hide]').onclick=hide;
    q('[data-expand]').onclick=()=>{expanded=!expanded;layout.classList.toggle('panel-expanded',expanded);q('[data-expand]').setAttribute('aria-label',expanded?'Restore panel size':'Expand workspace panel');};
    function outside(e){if(!panel.contains(e.target))menu(false);}document.addEventListener('pointerdown',outside);
    panel.addEventListener('keydown',e=>{if(e.key==='Escape'&&!q('.panel-menu').hidden){menu(false);q('[data-add]').focus();}});
    function make(kind,data={}){
      if(kind==='chat'&&data.chatId){const existing=[...tabs.values()].find(t=>t.chatId===data.chatId);if(existing)return existing;}
      const id=data.id||(kind==='file'?'file:'+data.path:kind==='chat'?'chat:'+(data.chatId||crypto.randomUUID()):kind);
      if(tabs.has(id))return tabs.get(id);if(tabs.size>=16){toast('Close a tab before opening another.');return null;}
      const t={...data,id,kind,uid:crypto.randomUUID(),title:data.title||({computer:'Computer',files:'Files',chat:'Side chat',activity:'Activity'}[kind]||'File')};
      t.element=document.createElement('section');t.element.className='panel-view panel-'+kind;t.element.id='view-'+t.uid;t.element.setAttribute('role','tabpanel');t.element.setAttribute('aria-labelledby','tab-'+t.uid);t.element.hidden=true;views.append(t.element);tabs.set(id,t);return t;
    }
    function open(kind,data={}){const t=make(kind,data);if(t)select(t.id);return true;}
    function select(id){
      if(disposed||!tabs.has(id))return;
      const previous=tabs.get(active);if(previous?.id===id&&visible)return;
      previous?.deactivate?.();if(previous)previous.element.hidden=true;
      active=id;const t=tabs.get(id);t.element.hidden=false;setVisible(true);
      if(!t.loaded){t.loaded=true;mount(t);}
      t.activate?.();draw();save();q('[aria-selected="true"]')?.scrollIntoView({block:'nearest',inline:'nearest'});
    }
    function remove(id){const t=tabs.get(id);if(!t)return;t.deactivate?.();t.dispose?.();if(t.kind==='activity')parking.append(activity);t.element.remove();tabs.delete(id);if(id===active){active='';const next=[...tabs.keys()].at(-1);if(next)select(next);else hide();}draw();save();}
    function good(t){return !disposed&&tabs.get(t.id)===t;}
    function error(t,e){if(good(t))t.element.innerHTML=`<div class="panel-empty" role="alert">${esc(e.message)}<p><button type="button" data-retry>Retry</button></p></div>`;t.element.querySelector('[data-retry]')?.addEventListener('click',()=>mount(t));}
    function loading(t){t.element.innerHTML='<div class="panel-empty" role="status">Loading…</div>';}
    async function mount(t){
      const epoch=t.epoch=(t.epoch||0)+1;const current=()=>good(t)&&epoch===t.epoch;
      if(t.kind==='computer'){t.activate=()=>computer.open(run.id,t.element);t.deactivate=()=>computer.close();return;}
      if(t.kind==='activity'){t.element.append(activity);return;}
      if(t.kind==='chat'){mountChat(t);return;}
      loading(t);
      try{
        const catalog=await api(`/api/runs/${run.id}/files`);if(!current())return;
        if(t.kind==='files'){
          t.element.innerHTML='<div class="panel-file-search"><input type="search" aria-label="Find a saved file" placeholder="Find a file…"><button type="button" data-refresh aria-label="Refresh saved files">↻</button></div><div class="panel-file-list"></div><p class="panel-footnote">Latest saved version · Select a file to open it in a tab.</p>';
          t.expandedFolders??=new Set();
          const input=t.element.querySelector('input');input.value=t.fileSearch||'';
          function list(){
            t.fileSearch=input.value;const term=input.value.toLowerCase(),files=catalog.files.filter(f=>f.path.toLowerCase().includes(term));
            t.element.querySelector('.panel-file-list').innerHTML=renderFileTree(files,{escape:esc,size,expanded:t.expandedFolders,search:!!term});
            t.element.querySelectorAll('[data-folder]').forEach(folder=>folder.ontoggle=()=>{
              // Search temporarily opens ancestors; don't overwrite browsing state.
              if(term||!folder.isConnected)return;
              if(folder.open)t.expandedFolders.add(folder.dataset.folder);else t.expandedFolders.delete(folder.dataset.folder);
            });
            t.element.querySelectorAll('[data-file]').forEach(b=>b.onclick=()=>openFile(files[Number(b.dataset.file)]));
          }
          t.element.querySelector('input').oninput=list;t.element.querySelector('[data-refresh]').onclick=()=>mount(t);list();return;
        }
        const file=catalog.files.find(f=>f.archive_path===t.path);if(!file)throw new Error('This file is no longer in the latest saved workspace. Open Files to choose another.');
        t.title=file.name;draw();t.element.innerHTML=`<header class="panel-file-heading"><div><strong>${esc(file.name)}</strong><small>${esc(file.path)}</small></div><a href="${esc(file.url)}" download="${esc(file.name)}">↓ Download</a><button type="button" data-refresh aria-label="Refresh file">↻</button></header><div class="panel-file-content"></div>`;
        const content=t.element.querySelector('.panel-file-content');t.element.querySelector('[data-refresh]').onclick=()=>mount(t);
        if(file.inline_url&&['image','video'].includes(file.kind)){content.classList.add('panel-media');content.innerHTML=file.kind==='video'?`<video controls preload="metadata" src="${esc(file.inline_url)}"></video>`:`<img src="${esc(file.inline_url)}" alt="${esc(file.name)}">`;}
        else{const result=await api(file.preview_url);if(!current()||!content.isConnected)return;content.innerHTML=result.text===null?'<p class="panel-empty">No preview for this file type. Download it to open it.</p>':`${result.truncated?'<p class="saved-file-notice">Showing the first 128 KB. Download for the complete file.</p>':''}${result.format==='markdown'?`<div class="markdown">${markdown(result.text)}</div>`:`<pre class="saved-file-text">${esc(result.text)}</pre>`}`;decorate(content,catalog.files);}
        t.deactivate=()=>t.element.querySelectorAll('video').forEach(v=>v.pause());
      }catch(e){if(current())error(t,e);}
    }
    function openFile(file){return open('file',{path:file.archive_path,title:file.name});}
    function decorate(element,files){
      MoyaiFiles.decorate(element,files,{onOpen:openFile});
      element.querySelectorAll('.copy-code').forEach(b=>b.onclick=async()=>{try{await navigator.clipboard.writeText(b.closest('.code-block').querySelector('code').textContent);b.textContent='Copied';}catch{b.textContent='Select to copy';}});
    }
    function mountChat(t){
      t.element.innerHTML=`<div class="side-chat-note"><span>Separate conversation · Main task keeps running</span><a data-full hidden target="_blank" rel="noopener">Open session ↗</a></div><div class="side-chat-messages" role="log" aria-label="Side conversation"></div><form class="side-chat-form"><p data-status role="status"></p><label class="sr-only" for="side-input-${t.uid}">Message side chat</label><textarea id="side-input-${t.uid}" placeholder="Ask about this session…" rows="3" maxlength="16000" required></textarea><div><select aria-label="Side chat model">${models.map(m=>`<option value="${esc(m.id)}" ${m.id===(t.model||run.model)?'selected':''}>${esc(m.name)}</option>`).join('')}</select><button type="button" data-stop hidden>Stop</button><button type="submit" aria-label="Send side chat message">↑</button></div><small>Starts with a snapshot of this conversation. Files and computer are separate.</small></form>`;
      const form=t.element.querySelector('form'),input=form.querySelector('textarea'),log=t.element.querySelector('[role="log"]'),status=form.querySelector('[data-status]'),model=form.querySelector('select'),send=form.querySelector('[type="submit"]'),stop=form.querySelector('[data-stop]'),link=t.element.querySelector('[data-full]');
      let timer,inFlight=false,enabled=false,unavailable=false,signature='';
      model.onchange=()=>{t.model=model.value;save();};
      input.value=t.draft||'';input.oninput=()=>{t.draft=input.value;save();};
      function showEmpty(){if(!t.chatId&&!unavailable)log.innerHTML='<div class="side-chat-empty"><span aria-hidden="true">◌</span><h3>Ask about this session</h3><p>Ask a question, explore another idea, or discuss the work without interrupting Moyai.</p></div>';}
      function failed(e){
        if(!good(t)||unavailable)return;
        if(e.status!==404){status.textContent=e.message;return;}
        unavailable=true;clearTimeout(timer);signature='';status.textContent='';
        log.innerHTML='<div class="panel-empty" role="status">This side chat is no longer available.</div>';
        input.disabled=model.disabled=send.disabled=stop.disabled=true;stop.hidden=link.hidden=true;
      }
      function drawChat(data){
        syncTitles([data]);const transcript=MoyaiQueue.presentation(data).transcript,next=JSON.stringify(transcript);const bottom=log.scrollHeight-log.scrollTop-log.clientHeight<100;
        if(signature!==next){
          signature=next;const slots=new Map([...log.querySelectorAll('[data-activity-slot]')].map(slot=>[slot.dataset.activitySlot,slot]));
          log.innerHTML=transcript.map(m=>`<article class="side-message ${m.role==='user'?'from-user':''}"><div>${m.role==='user'?'You':'Moyai'}</div><div class="${m.role==='user'?'plain-text':'markdown'}">${m.role==='user'?esc(m.display_content??m.content):markdown(m.content)}</div></article>${m.role==='user'?`<div data-activity-slot="${m.id}"></div>`:''}`).join('');
          log.querySelectorAll('[data-activity-slot]').forEach(slot=>{const previous=slots.get(slot.dataset.activitySlot);if(previous)slot.replaceWith(previous);});
        }
        MoyaiActivity.sync(log,data,{markdown,copy:async(text)=>{try{await navigator.clipboard.writeText(text);}catch{toast('Select the text to copy.');}}});MoyaiActivity.tick(log);if(bottom)log.scrollTop=log.scrollHeight;
        const working=!['idle','completed','failed','cancelled','interrupted'].includes(data.status)||data.active;
        const queued=data.messages.filter(m=>m.role==='user'&&m.status==='queued').length;
        status.textContent=data.error||({queued:'Waiting to start…',provisioning:'Opening side-chat workspace…',running:MoyaiActivity.current(data).headline,saving:'Saving…',reconnecting:'Reconnecting…'}[data.status])||'';
        if(working&&queued)status.textContent+=(status.textContent?' · ':'')+queued+' queued';
        stop.hidden=!working;send.title=working?'Queue side message':'Send side message';link.hidden=false;link.href='/#run='+t.chatId;
        if(data.approvals?.some(a=>a.status==='pending')||data.credential_requests?.length)status.textContent='Action needed. Open this side chat as a full session to continue.';
      }
      async function poll(){clearTimeout(timer);if(!enabled||unavailable||!good(t)||!t.chatId||inFlight||document.hidden)return;inFlight=true;const chatId=t.chatId;try{const data=await api('/api/runs/'+chatId);if(good(t)&&!unavailable&&t.chatId===chatId)drawChat(data);}catch(e){if(t.chatId===chatId)failed(e);}finally{inFlight=false;if(enabled&&!unavailable&&good(t))timer=setTimeout(poll,2000);}}
      t.activate=()=>{enabled=true;showEmpty();poll();};t.deactivate=()=>{enabled=false;clearTimeout(timer);};t.dispose=t.deactivate;
      function visibility(){if(!document.hidden&&enabled)poll();}document.addEventListener('visibilitychange',visibility);t.dispose=()=>{t.deactivate();document.removeEventListener('visibilitychange',visibility);};
      form.onsubmit=async e=>{
        e.preventDefault();const text=input.value.trim();if(!text||send.disabled)return;send.disabled=true;input.disabled=true;status.textContent='Sending…';
        const body={...(t.chatId?{content:text}:{prompt:text,mode:run.mode,repo_url:run.repo_url,environment_id:run.environment_id||'auto',harness:run.harness||'hermes',plugins:run.plugins,side_chat_of:run.id}),model:model.value||run.model};
        const submission=JSON.stringify(body);if(t.submission!==submission||!t.clientId){t.clientId=crypto.randomUUID();t.submission=submission;}save();
        try{
          if(!t.chatId){const created=await api('/api/runs',{method:'POST',body:JSON.stringify({...body,client_id:t.clientId})});if(!good(t)||unavailable)return;t.chatId=created.id;t.title=text.length>28?text.slice(0,28)+'…':text;draw();sideChats.push(created);onCreated?.();}
          else await api(`/api/runs/${t.chatId}/messages`,{method:'POST',body:JSON.stringify({...body,client_id:t.clientId})});
          if(!good(t)||unavailable)return;
          t.draft='';t.clientId='';t.submission='';input.value='';save();await poll();if(good(t)&&!unavailable){input.disabled=false;input.focus();}
        }catch(e){failed(e);}finally{send.disabled=unavailable;input.disabled=unavailable;}
      };
      input.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!send.disabled)form.requestSubmit();}};
      stop.onclick=async()=>{if(unavailable||!good(t))return;stop.disabled=true;try{await api(`/api/runs/${t.chatId}/cancel`,{method:'POST'});await poll();}catch(e){failed(e);}finally{stop.disabled=unavailable;}};
      showEmpty();
    }
    const {titleFor=(r)=>(r.parent_run_id?r.agent_label||r.display_title:r.display_title||r.agent_label)||r.prompt,matchesSession=(r,s)=>(r.prompt||'').toLowerCase().includes(s)}=arguments[0];
    // Extend the upstream tab menu without replacing its file-tree setup.
    drawMenu=function(){
      const items=menuItems(q('.panel-menu input').value.toLowerCase());
      q('[data-menu-items]').innerHTML=renderMenuItems(items);
      q('[data-menu-items]').querySelectorAll('button').forEach(b=>b.onclick=()=>{const item=items[Number(b.dataset.item)];open(item.kind,item.chatId?{chatId:item.chatId,title:item.title}:{});menu(false);});
    };
    q('.panel-menu input').oninput=drawMenu;
    function renderMenuItems(items){
      return items.map((item,i)=>`<button type="button" data-item="${i}"><span class="panel-tab-icon">${ico(glyph[item.kind],16)}</span><span><strong>${esc(item.title)}</strong><small>${esc(item.detail)}</small></span></button>`).join('')||'<p class="panel-empty">No matching tabs.</p>';
    }
    function menuItems(search){
      // Search both the saved original request and its display title.
      return [...(run.mode==='modal'?[{kind:'computer',title:'Computer',detail:'Watch and use the sandbox desktop'}]:[]),{kind:'files',title:'Files',detail:'Open saved files, screenshots, and videos'},{kind:'chat',title:'Side chat',detail:'A separate conversation about this session'},{kind:'activity',title:'Activity',detail:'Tools, approvals, and session details'},...sideChats.filter(c=>matchesSession(c,search)).map(c=>({kind:'chat',title:titleFor(c),detail:'Saved side chat',chatId:c.id}))].filter(i=>i.chatId||i.title.toLowerCase().includes(search));
    }
    function syncTitles(rows){
      // Do not replace chat tabs or drafts when a background title arrives.
      if(disposed)return;
      const byId=new Map(rows.map(row=>[row.id,row]));
      sideChats=sideChats.map(chat=>byId.has(chat.id)?{...chat,...byId.get(chat.id)}:chat);
      let changed=false;
      for(const tab of tabs.values()){
        const row=byId.get(tab.chatId);if(!row)continue;
        const title=titleFor(row);if(tab.title!==title){tab.title=title;changed=true;}
      }
      if(changed){draw();save();}
      if(!q('.panel-menu').hidden)drawMenu();
    }
    initial.tabs.forEach(t=>{if(t.kind!=='computer'||run.mode==='modal')make(t.kind,t);});
    if(initial.visible&&tabs.size)select(tabs.has(initial.active)?initial.active:tabs.keys().next().value);else{active=initial.active;draw();}
    restoring=false;save();
    api(`/api/runs/${run.id}/side-chats`).then(rows=>{if(!disposed){sideChats=rows;syncTitles(rows);}}).catch(()=>{});
    return {open,openFile,hide,syncTitles,toggle(){if(visible)hide();else if(tabs.size)select(tabs.has(active)?active:tabs.keys().next().value);else open(run.mode==='modal'?'computer':'files');},dispose(){disposed=true;tabs.forEach(t=>{t.deactivate?.();t.dispose?.();});document.removeEventListener('pointerdown',outside);panel.remove();layout.classList.remove('panel-open','panel-expanded');}};
  }
  return {create,restore,fileTree,renderFileTree};
});
