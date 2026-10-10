/* A bounded room over saved public swarm messages, not a second conversation runtime. */
(function(root){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const identifier=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
  const pending=new Set(['saving','queued','running','injected','streaming']);
  const kinds=new Set(['human','assistant','leadership','assignment','message']);
  const order=(a,b)=>(Date.parse(a.created_at)||0)-(Date.parse(b.created_at)||0)||a.id.localeCompare(b.id);
  function publicItems(items){
    return (Array.isArray(items)?items:[]).filter(item=>typeof item?.id==='string'&&identifier(item.run_id)&&kinds.has(item.kind)&&typeof item.content==='string'&&item.author&&['human','coordinator','agent'].includes(item.author.role));
  }
  function store({runId,api,onChange=()=>{},active=()=>true,following=()=>true,now=()=>Date.now()}){
    let pages=[],live=[],local=[],loaded=false,older=false,history=false,error='',denied=false,disposed=false,request=null,generation=0,watchOffset=0,next=null,due=0,lastMode='latest';
    function snapshot(){
      const items=new Map();for(const page of pages)for(const item of page.items)items.set(item.id,item);
      if(!history){
        for(const item of live)if(!items.has(item.id)||pending.has(items.get(item.id).status))items.set(item.id,{...item,live:true});
        for(const item of local)if(!items.has(item.id))items.set(item.id,item);
      }
      return {items:[...items.values()].sort(order),loaded,older,history,error,denied,loading:!!request};
    }
    function changed(mode){if(!disposed)onChange(snapshot(),mode);}
    async function load(mode){
      if(disposed||denied||request||!active())return;
      if(mode==='before'&&(!older||!pages[0]?.previous))return;
      const cursor=mode==='before'?pages[0].previous:mode==='after'?next:null;
      if(mode==='after'&&!loaded)mode='latest';
      const token={generation,controller:new AbortController()};request=token;lastMode=mode;error='';changed('status');
      try{
        const query=new URLSearchParams({limit:'50'});if(cursor)query.set(mode==='before'?'before':'after',cursor);
        const mutable=snapshot().items.filter(item=>!item.id.startsWith('board:')&&pending.has(item.status)).map(item=>item.id);
        const start=watchOffset%Math.max(1,mutable.length),watch=[...mutable.slice(start),...mutable.slice(0,start)].slice(0,100);watchOffset=start+watch.length;
        if(watch.length)query.set('watch',watch.join(','));
        const result=await api(`/api/runs/${runId}/swarm/chat?${query}`,{signal:token.controller.signal});
        if(disposed||token.generation!==generation||!active())return;
        const priorLive=new Set(live.map(item=>item.id));
        const updates=new Map(publicItems(result.updated_items).map(item=>[item.id,item])),removed=new Set(Array.isArray(result.removed_ids)?result.removed_ids:[]);
        const revise=items=>items.filter(item=>!removed.has(item.id)).map(item=>updates.get(item.id)||item);
        pages=pages.map(page=>({...page,items:revise(page.items)}));live=revise(live);local=revise(local);
        const page={items:publicItems(result.items).slice(0,50),previous:result.previous_cursor||null};
        if(mode==='latest'){pages=[page];history=false;older=!!result.has_more;}
        else if(mode==='before'){pages=[page,...pages].slice(0,4);history=true;older=!!result.has_more;}
        else if(page.items.length){pages.push(page);if(pages.length>4){pages=pages.slice(-4);older=true;}}
        if(mode!=='before'){next=result.next_cursor||null;live=publicItems(result.live_items).slice(-100);}
        const saved=new Set(pages.flatMap(page=>page.items.map(item=>item.id)));
        local=[...new Map([...local,...[...updates.values()].filter(item=>priorLive.has(item.id))].filter(item=>!saved.has(item.id)&&!removed.has(item.id)).map(item=>[item.id,item])).values()].slice(-50);
        loaded=true;due=now()+(mode==='after'&&result.has_more?1000:4000);changed(mode);
      }catch(failure){
        if(disposed||token.generation!==generation||failure?.name==='AbortError')return;
        denied=[401,403,404].includes(failure?.status);
        if(denied){pages=[];live=[];local=[];next=null;older=false;error='This team chat is no longer available.';}
        else error='Couldn’t refresh team messages.';
        due=now()+8000;changed('error');
      }finally{if(request===token){request=null;if(!disposed&&token.generation===generation)changed('status');}}
    }
    return {snapshot,inject(items){if(disposed||denied)return;local=[...new Map([...local,...publicItems(items)].map(item=>[item.id,item])).values()].slice(-50);changed('local');},earlier:()=>load('before'),latest:()=>load('latest'),
      poll(){if(!history&&following()&&now()>=due)return load(loaded?'after':'latest');},
      retry(){due=0;return load(lastMode);},
      pause(){generation++;request?.controller.abort();request=null;due=0;},
      dispose(){disposed=true;generation++;request?.controller.abort();request=null;pages=[];live=[];local=[];},
    };
  }
  function messageHTML(item,{harnessName=id=>id||'Harness unavailable',modelName=id=>id||'',markdown=esc,attachments=()=>''}={}){
    const author=item.author,name=author.role==='coordinator'?'Coordinator':author.name||'Agent';
    const human=author.role==='human',logo=!human&&root.MoyaiProviderLogos?.harness(author.harness);
    const avatar=logo?`<img src="${esc(logo)}" alt="" width="28" height="28">`:`<span aria-hidden="true">${esc(name.slice(0,1).toUpperCase())}</span>`;
    const identity=human?`<span class="swarm-chat-author">${esc(name)}</span>`:`<button type="button" class="quiet swarm-chat-author" data-room-agent="${esc(item.run_id)}" aria-label="Open ${esc(name)} conversation">${esc(name)}</button>`;
    const stamp=Date.parse(item.created_at),time=Number.isFinite(stamp)?`<time datetime="${esc(item.created_at)}" title="${esc(new Date(stamp).toLocaleString())}">${esc(new Date(stamp).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}))}</time>`:'';
    const kind=item.kind==='leadership'?'Became lead':item.kind==='assignment'?'Assignment':item.kind==='message'?'Shared message':'';
    const target=item.target&&identifier(item.target.id)?`<button type="button" class="quiet swarm-chat-target" data-room-agent="${esc(item.target.id)}" aria-label="Open ${esc(item.target.name)} conversation">→ ${esc(item.target.name)}</button>`:'';
    const status=item.status==='save_failed'?'Answer saved · workspace save failed':item.live?'Live update':item.status==='saving'?'Saving reply':['failed','cancelled','interrupted'].includes(item.status)?'Response '+item.status:'';
    return `<article class="swarm-chat-message ${human?'swarm-chat-human':''}" data-region-key="room:${esc(item.id)}" data-room-id="${esc(item.id)}" data-message-run="${esc(item.run_id)}"><div class="swarm-chat-identity" data-region-key="identity" data-region-leaf><span class="swarm-chat-avatar">${avatar}</span><div class="swarm-chat-byline"><div>${identity}${author.is_leader?'<span class="swarm-chat-lead">Lead</span>':''}${time}</div>${human?'':`<p>${esc(harnessName(author.harness))}${author.model?`<span> · ${esc(modelName(author.model))}</span>`:''}</p>`}</div></div><div class="swarm-chat-content" data-region-key="content" data-region-leaf><div class="message-content ${human?'plain-text':'markdown'}">${human?esc(item.content):markdown(item.content)}</div>${attachments(item)}</div><div class="swarm-chat-message-meta" data-region-key="meta" data-region-leaf>${kind?`<span>${esc(kind)}</span>`:''}${target}${item.truncated?`<button type="button" class="quiet swarm-chat-target" data-room-agent="${esc(item.run_id)}">Read full message</button>`:''}${status?`<span>${esc(status)}</span>`:''}</div></article>`;
  }
  function create({host,run:initial,api,harnessName,modelName,markdown,attachments=()=>'',onAgent=()=>{},onFile=()=>{},onAttachment=()=>{},onActivity=()=>{},warning=()=>'',publicMessages=run=>run.messages||[],copy=()=>{},decorate=()=>{},jump}){
    let seen=new Set(publicMessages(initial).map(message=>message.id));
    let run=initial,warningHTML='',visible=false,disposed=false,painted='',lastSnapshot=null,scrolled=false;
    host.classList.add('swarm-chat');host.setAttribute('aria-label','Swarm group chat');
    root.MoyaiUI.render(host,'<div class="conversation-inner swarm-chat-inner"><header class="swarm-chat-heading"><div><h2>Team chat</h2><p>Your messages guide the coordinator.</p></div><span data-room-count></span></header><div class="swarm-chat-history"><button type="button" class="quiet" data-room-earlier hidden>Load earlier messages</button><button type="button" class="quiet" data-room-latest hidden>Back to latest</button></div><div class="swarm-chat-notice" role="status"><span data-room-notice>Open Chat to see the team’s messages.</span><button type="button" class="quiet" data-room-retry hidden>Retry</button></div><div data-room-warning></div><div data-room-messages></div><div id="credential-requests"></div></div>');
    const q=selector=>host.querySelector(selector),messages=q('[data-room-messages]');
    const nearBottom=()=>host.scrollHeight-host.scrollTop-host.clientHeight<120;
    const active=()=>visible&&!disposed&&!document.hidden;
    function anchor(){const top=host.getBoundingClientRect().top;const node=[...messages.children].find(node=>node.getBoundingClientRect().bottom>top);return node?{id:node.dataset.roomId,offset:node.getBoundingClientRect().top-top}:null;}
    function draw(snapshot,mode){
      if(disposed)return;lastSnapshot=snapshot;
      const follow=!scrolled||nearBottom(),position=anchor(),oldTop=host.scrollTop;
      const transcriptIds=new Set(publicMessages(run).map(message=>`message:${message.id}`));
      const visibleItems=snapshot.items.filter(item=>!(item.kind==='human'&&item.run_id===run.id&&item.status==='queued'&&!transcriptIds.has(item.id)));
      const html=visibleItems.map(item=>messageHTML(item,{harnessName,modelName,markdown,attachments:item=>attachments(item,run)})).join('');
      if(html!==painted){painted=html;root.MoyaiRegions.sync(messages,html);decorateFiles();decorate();
        if((mode==='latest'||follow&&mode!=='before')&&!document.activeElement?.closest('#credential-requests'))host.scrollTop=host.scrollHeight;
        else if(position){const node=[...messages.children].find(node=>node.dataset.roomId===position.id);if(node)host.scrollTop=oldTop+node.getBoundingClientRect().top-host.getBoundingClientRect().top-position.offset;}
      }
      q('[data-room-earlier]').hidden=!snapshot.older;q('[data-room-earlier]').disabled=snapshot.loading;
      q('[data-room-latest]').hidden=!snapshot.history;q('[data-room-latest]').disabled=snapshot.loading;
      q('[data-room-retry]').hidden=!snapshot.error||snapshot.denied;q('[data-room-retry]').disabled=snapshot.loading;
      q('[data-room-notice]').textContent=snapshot.error||(!snapshot.loaded?'Loading team messages…':!snapshot.items.length?'The team’s messages will appear here.':'');
      syncJump();
    }
    const feed=store({runId:run.id,api,onChange:draw,active,following:()=>!scrolled||nearBottom()});
    function syncJump(){if(jump){jump.textContent='↓ Latest messages';jump.hidden=!visible||(!lastSnapshot?.history&&nearBottom());}}
    function decorateFiles(){
      for(const link of messages.querySelectorAll('[data-file-ref]')){
        const owner=link.closest('[data-message-run]')?.dataset.messageRun;if(!identifier(owner))continue;
        link.dataset.roomFile=owner;link.href='#run='+owner;link.removeAttribute('aria-disabled');link.title='Open file in this agent’s workspace';
      }
    }
    function click(event){
      if(event.defaultPrevented)return;
      const button=event.target.closest('button'),link=event.target.closest('[data-room-file]');
      if(link&&host.contains(link)&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey&&!event.altKey&&!event.button){event.preventDefault();onFile(link.dataset.roomFile,link.dataset.fileRef);return;}
      if(!button||!host.contains(button))return;
      if(button.dataset.roomAgent){const item=lastSnapshot?.items.find(item=>item.run_id===button.dataset.roomAgent);onAgent({id:button.dataset.roomAgent,label:item?.author.name||button.textContent.replace(/^→ /,'')});}
      else if(button.hasAttribute('data-room-earlier'))feed.earlier();
      else if(button.hasAttribute('data-room-latest'))latest();
      else if(button.hasAttribute('data-room-retry'))feed.retry();
      else if(button.hasAttribute('data-room-activity'))onActivity();
      else if(button.hasAttribute('data-attachment'))onAttachment(button.dataset.attachment);
      else if(button.matches('.copy-code'))copy(button.closest('.code-block').querySelector('code').textContent,button);
    }
    function latest(){scrolled=false;host.scrollTop=host.scrollHeight;return feed.latest();}
    function scroll(){scrolled=true;syncJump();}
    function visibility(){if(document.hidden)feed.pause();else if(visible)feed.poll();}
    host.addEventListener('click',click);host.addEventListener('scroll',scroll);document.addEventListener('visibilitychange',visibility);
    const timer=setInterval(()=>{if(active())feed.poll();},4000);
    function update(next){
      if(disposed||next.id!==run.id)return;run=next;
      const html=warning(run);if(html!==warningHTML){warningHTML=html;root.MoyaiUI.render(q('[data-room-warning]'),html);}
      q('[data-room-count]').textContent=`${Math.max(0,(root.MoyaiSwarm?.members(run).length||1)-1)} agents`;
      const fresh=publicMessages(run).filter(message=>!seen.has(message.id));seen=new Set(publicMessages(run).map(message=>message.id));
      const humans=fresh.filter(message=>message.role==='user'&&(!message.input_kind||message.input_kind==='user')&&!message.pending&&message.source!=='swarm'&&!String(message.client_id||'').startsWith('swarm:'));
      if(humans.length)feed.inject(humans.map(message=>({id:`message:${message.id}`,run_id:run.id,kind:'human',created_at:message.created_at||new Date().toISOString(),content:message.content,status:message.status,author:{id:message.user_id,name:message.user_name||'You',role:'human'}})));
      else if(lastSnapshot)draw(lastSnapshot,'status');
    }
    update(run);
    return {update,latest,setVisible(value){visible=!!value;if(!visible)feed.pause();else feed.poll();syncJump();},
      dispose(){disposed=true;feed.dispose();clearInterval(timer);host.removeEventListener('click',click);host.removeEventListener('scroll',scroll);document.removeEventListener('visibilitychange',visibility);},
    };
  }
  const api={store,messageHTML,publicItems,create};root.MoyaiSwarmChat=api;if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);
