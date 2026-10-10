/* A view of saved Moyai sessions, never a second agent runtime. */
(function(root){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const identifier=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
  const working=new Set(['running','provisioning','saving','reconnecting','queued','waiting_children']);
  const attention=new Set(['failed','interrupted','awaiting_approval','waiting_credential']);
  const labels={idle:'Ready',completed:'Completed',running:'Working',provisioning:'Starting',queued:'Queued',saving:'Saving',reconnecting:'Reconnecting',waiting_children:'Agents working',awaiting_approval:'Needs approval',waiting_credential:'Needs access',failed:'Failed',interrupted:'Interrupted',cancelled:'Stopped',stopping:'Stopping',deleting:'Deleting'};
  const duration=seconds=>{const value=Math.max(0,Math.ceil(Number(seconds)||0));return value>=3600?`${Math.floor(value/3600)}h ${Math.floor(value%3600/60)}m`:value>=60?`${Math.floor(value/60)}m ${value%60}s`:`${value}s`;};
  function members(run){
    if(!identifier(run?.id))return [];
    const nodes=new Map([[run.id,{...run,label:'Coordinator',parentId:null,depth:0}]]);
    function visit(children,parentId,depth){
      for(const child of children||[]){
        if(!identifier(child?.id)||nodes.has(child.id))continue;
        nodes.set(child.id,{...child,label:child.agent_label||child.display_title||'Agent',parentId,depth});
        visit(child.children,child.id,depth+1);
      }
    }
    for(const group of run.agents?.groups||[])visit(group.children,run.id,1);
    return [...nodes.values()];
  }
  function layout(nodes,limit=10,narrow=false){
    if(!nodes.length)return [];
    // Keep active/attention nodes visible; the existing Subagents panel exposes
    // the complete hierarchy when a large team exceeds the visual canvas.
    const children=nodes.slice(1).map((node,index)=>({node,index})).sort((a,b)=>
      Number(attention.has(b.node.status))-Number(attention.has(a.node.status))||
      Number(working.has(b.node.status))-Number(working.has(a.node.status))||a.index-b.index).slice(0,limit).map(item=>item.node);
    const dense=children.length>4;
    const positions=narrow?(children.length===1?[[.5,.48]]:children.map((_,index)=>[
      children.length%2===1&&index===children.length-1?.5:index%2===0?.25:.75,
      dense?.30+Math.floor(index/2)*.11:index<2?.40:.59])):
      children.length===1?[[.5,.54]]:children.length===2?[[.27,.54],[.73,.54]]:
      children.length===3?[[.18,.54],[.5,.54],[.82,.54]]:children.length===4?[[.14,.54],[.38,.54],[.62,.54],[.86,.54]]:
      children.map((_,index)=>{const row=Math.floor(index/5),count=Math.min(5,children.length-row*5),column=index%5;return [count===1?.5:.10+(.80/(count-1))*column,row===0?.64:.88];});
    return [{...nodes[0],x:.5,y:dense?(narrow?.17:.40):(narrow?.20:.28)},...children.map((node,index)=>({...node,x:positions[index][0],y:positions[index][1]}))];
  }
  function clock(run,now=Date.now()){
    const swarm=run.swarm;if(!swarm)return null;
    const deadline=typeof swarm.ends_at==='string'?Date.parse(swarm.ends_at):NaN;
    const remaining=Number.isFinite(deadline)?Math.max(0,Math.ceil((deadline-now)/1000)):null;
    const status=String(swarm.status||'');
    const ended=attention.has(run.status)&&status==='active';
    const label=ended?'Needs attention':({active:remaining===0?'Wrapping up':'Working together',paused:'Paused',stopped:'Stopped',expired:'Time limit reached',blocked:'Needs attention'})[status]||'Swarm';
    const timing=status==='active'&&!ended&&remaining!==null?`Up to ${duration(remaining)} left`:Number(swarm.budget_seconds)>0?`${duration(swarm.budget_seconds)} maximum`:'';
    return {status:ended?'blocked':status,label,timing,remaining,round:Number.isInteger(swarm.round)&&swarm.round>0?swarm.round:null,reason:swarm.reason||''};
  }
  function canResume(run,now=Date.now()){
    const swarm=run?.swarm,deadline=Date.parse(swarm?.ends_at||'');
    return !!swarm&&['paused','blocked'].includes(swarm.status)&&Number.isFinite(deadline)&&deadline>now&&Number(swarm.round||0)<25;
  }
  function canSend(run,now=Date.now()){
    if(!run?.swarm)return true;
    const deadline=Date.parse(run.swarm.ends_at||'');
    return run.swarm.status==='active'&&(!Number.isFinite(deadline)||deadline>now);
  }
  function composerNote(run,now=Date.now()){
    if(canSend(run,now))return '';
    return canResume(run,now)?'Resume this swarm before sending. Your draft stays here.':'This swarm has ended. Start a new session to continue; your draft stays here.';
  }
  function receipt(run){
    const event=(run.events||[]).findLast(item=>['agents','tool','artifact'].includes(item.kind)&&typeof item.message==='string'&&item.message.trim()&&item.data?.phase!=='processing');
    return event?{id:event.id,text:event.message,kind:event.kind}:null;
  }
  function latestResponse(run){
    // Only public assistant prose belongs here. Tool payloads, focus/status
    // notices and native reasoning are intentionally not response sources.
    const answer=(run?.messages||[]).findLast(message=>message.role==='assistant'&&!['steered','deleted'].includes(message.status)&&typeof message.content==='string'&&message.content.trim());
    const update=(run?.events||[]).findLast(event=>event.kind==='message'&&(!event.data?.phase||event.data.phase==='commentary')&&typeof event.message==='string'&&event.message.trim());
    const answerAt=Date.parse(answer?.created_at||''),updateAt=Date.parse(update?.created_at||'');
    const newerUpdate=update&&(!answer||(Number.isFinite(updateAt)&&Number.isFinite(answerAt)?updateAt>answerAt:
      update.data?.turn_id&&String(update.data.turn_id)===String(run.active_message_id)&&String(answer.response_to_id)!==String(run.active_message_id)));
    if(newerUpdate)return {id:update.data?.activity_id||update.id,text:update.message.trim(),kind:'update',status:'commentary',createdAt:update.created_at};
    if(!answer)return null;
    return {id:answer.id,text:answer.content.trim(),kind:['failed','cancelled','interrupted'].includes(answer.status)?'error':'reply',status:answer.status,createdAt:answer.created_at};
  }
  function responseCache({api,onChange=()=>{},active=()=>true,now=()=>Date.now()}){
    const records=new Map(),settled=new Set(['idle','completed','failed','cancelled','interrupted']);
    let disposed=false,generation=0,inFlight=0,denied=false;
    const version=node=>JSON.stringify([node.status,node.updated_at||'']);
    const available=()=>!disposed&&!denied&&active();
    function changed(){if(!disposed&&active())onChange();}
    function sync(nodes){
      const ids=new Set(nodes.map(node=>node.id));
      for(const [id,record] of records)if(!ids.has(id)){record.request?.controller?.abort();records.delete(id);}
      for(const node of nodes){
        const key=version(node),record=records.get(node.id);
        if(!record){records.set(node.id,{key,response:null,loaded:false,request:null,due:0,settled:false,error:'',blocked:false});continue;}
        if(record.key!==key){record.key=key;record.due=0;record.settled=false;record.blocked=false;record.request?.controller?.abort();}
      }
    }
    function poll(){
      if(!available())return;
      for(const [id,record] of records){
        if(inFlight>=3)break;
        if(record.request||record.settled||record.blocked||record.due>now())continue;
        const request={generation,key:record.key,controller:typeof AbortController==='function'?new AbortController():null};
        record.request=request;inFlight++;
        Promise.resolve().then(()=>api(`/api/runs/${id}?activity=summary`,request.controller?{signal:request.controller.signal}:{})).then(snapshot=>{
          if(!available()||request.generation!==generation||records.get(id)!==record||record.key!==request.key)return;
          if(snapshot?.id!==id)throw new Error('Mismatched agent response');
          // Keep only the bounded public excerpt, never message histories or tool payloads.
          const response=latestResponse(snapshot);record.response=response?{...response,text:response.text.slice(0,2000)}:null;
          record.loaded=true;record.error='';record.settled=settled.has(snapshot.status)&&!snapshot.active;changed();
        }).catch(error=>{
          if(disposed||request.generation!==generation||records.get(id)!==record||record.key!==request.key||error?.name==='AbortError')return;
          if([401,403].includes(error?.status)){
            denied=true;generation++;
            for(const item of records.values()){item.response=null;item.loaded=false;item.error='unavailable';item.blocked=true;item.request?.controller?.abort();}
          }else if(error?.status===404){record.response=null;record.loaded=false;record.error='unavailable';record.blocked=true;}
          else record.error='retry';
          changed();
        }).finally(()=>{
          inFlight--;if(record.request===request){record.request=null;record.due=record.key===request.key?now()+4000:0;}
          poll();
        });
      }
    }
    function pause(){generation++;for(const record of records.values())record.request?.controller?.abort();}
    return {sync,poll,pause,get(id){const record=records.get(id);return record?{response:record.response,loading:!record.loaded&&!record.error,error:record.error}:null;},
      retry(id){const record=records.get(id);if(record&&!record.blocked){record.due=0;record.error='';record.settled=false;poll();changed();}},
      dispose(){disposed=true;pause();records.clear();}};
  }
  function responseHTML(node,state){
    const response=state?.response;
    if(!response){
      if(state?.error==='retry')return `<button type="button" class="quiet swarm-response swarm-response-error" data-swarm-retry="${esc(node.id)}"><span class="swarm-response-text">Couldn’t load reply</span><span class="swarm-response-meta">Retry</span></button>`;
      return `<span class="swarm-response-empty">${state?.error==='unavailable'?'Reply unavailable':state?.loading?'Loading reply…':'No reply yet'}</span>`;
    }
    const plain=response.text.replace(/\s+/g,' ').trim(),text=plain.length>260?plain.slice(0,259)+'…':plain;
    const label=response.kind==='update'?'Update':response.kind==='error'?'Stopped response':'Reply';
    return `<button type="button" class="quiet swarm-response" data-swarm-agent="${esc(node.id)}" aria-label="${esc(node.label+'. '+label+': '+text+'. Read full conversation.')}" title="Read full conversation"><span class="swarm-response-text">${esc(text)}</span><span class="swarm-response-meta">${label} · Read reply</span></button>`;
  }
  function sharedResponses(nodes,responseFor){
    return nodes.map(node=>({node,response:responseFor(node)})).filter(item=>item.response)
      .sort((a,b)=>(Date.parse(a.response.createdAt)||0)-(Date.parse(b.response.createdAt)||0)||a.node.id.localeCompare(b.node.id)).slice(-6);
  }
  function busHTML(items,harnessName=id=>id||'Harness unavailable'){
    if(!items.length)return '<p class="swarm-bus-empty" data-region-key="empty" data-region-leaf>Replies will appear here as the team works.</p>';
    return items.map(({node,response})=>{
      const plain=response.text.replace(/\s+/g,' ').trim(),text=plain.length>600?plain.slice(0,599)+'…':plain;
      const label=response.kind==='update'?'Live update':response.kind==='error'?'Stopped response':'Latest reply';
      return `<div class="swarm-bus-item" data-region-key="message:${esc(node.id)}:${esc(response.id)}" data-region-leaf><button type="button" class="quiet swarm-bus-message" data-swarm-agent="${esc(node.id)}" aria-label="${esc(node.label+'. '+label+'. Read full conversation.')}" title="Read full conversation"><span class="swarm-bus-identity"><strong>${esc(node.label)}</strong><span>${esc(harnessName(node.harness))}</span></span><span class="swarm-bus-text">${esc(text)}</span><span class="swarm-bus-meta">${label} · Read reply</span></button></div>`;
    }).join('');
  }
  function marker(status){
    if(['idle','completed'].includes(status))return '<path d="m3 6 2 2 4-4"/>';
    if(attention.has(status))return '<path d="M6 2.5v4M6 9v.1"/>';
    if(['cancelled','stopping','deleting'].includes(status))return '<rect x="3.5" y="3.5" width="5" height="5" rx=".5"/>';
    return '<circle cx="6" cy="6" r="2.5"/>';
  }
  function nodeHTML(node,{selectedId,harnessName=id=>id||'Harness unavailable',modelName=id=>id||'Model unavailable',response}={}){
    const logo=root.MoyaiProviderLogos?.harness(node.harness);
    const status=labels[node.status]||'Status unavailable';
    const harness=harnessName(node.harness),model=modelName(node.active_model||node.model);
    const title=[node.label,harness,model,status,node.prompt||node.task||''].filter(Boolean).join(' · ');
    return `<div class="swarm-node-position" data-region-key="node:${esc(node.id)}" style="left:${node.x*100}%;top:${node.y*100}%;--swarm-reply-width:${node.replyWidth||210}px"><div data-region-key="control:${esc(node.id)}" data-region-leaf><button type="button" class="quiet swarm-node" data-swarm-agent="${esc(node.id)}" data-status="${esc(node.status)}" aria-pressed="${selectedId===node.id}" aria-label="${esc(title+'. Open conversation and activity.')}" title="${esc(title)}"><span class="swarm-node-mark">${logo?`<img src="${esc(logo)}" alt="" width="38" height="38">`:`<span class="swarm-node-fallback" aria-hidden="true">${esc(String(node.label).slice(0,1))}</span>`}<span class="swarm-node-state" aria-hidden="true"><svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round">${marker(node.status)}</svg></span></span><span class="swarm-node-label">${esc(node.label)}</span><span class="swarm-node-harness">${esc(harness)}</span><span class="swarm-node-status">${esc(status)}</span></button></div><div class="swarm-node-response" data-region-key="response:${esc(node.id)}" data-region-leaf>${responseHTML(node,response)}</div></div>`;
  }
  function create({host,layout:container,run:initial,initialView,onView=()=>{},onAgent=()=>{},onActivity=()=>{},onAllAgents=()=>{},titleFor=run=>run.display_title||run.prompt||'Session',harnessName,modelName,api}){
    let run=initial,disposed=false,selectedId='',view=initialView||(run.swarm?'space':'chat'),graph=[],nodeSignature='',receiptSignature='',clockSignature='',busSignature='';
    root.MoyaiUI.render(host, `<div class="swarm-stage"><canvas class="swarm-canvas" aria-hidden="true"></canvas><header class="swarm-mission"><p class="swarm-kicker"></p><h2 tabindex="-1"></h2><p class="swarm-state-line"><span class="swarm-state-label"></span><time class="swarm-clock"></time></p><p class="swarm-reason" hidden></p></header><div class="swarm-nodes" role="group" aria-label="Session agents. Lines show delegation; signals show active workers."></div><div class="swarm-evidence"><p class="swarm-receipt" aria-live="polite"></p><div class="swarm-evidence-actions"><button type="button" class="quiet" data-swarm-activity>View activity</button><button type="button" class="quiet" data-swarm-all hidden></button></div></div></div><section class="swarm-bus" aria-label="Shared messages"><h3>Shared messages</h3><div class="swarm-bus-messages" role="log" aria-live="polite" aria-relevant="additions text"></div></section>`);
    const q=selector=>host.querySelector(selector);
    const canvas=q('canvas'),nodeHost=q('.swarm-nodes'),busHost=q('.swarm-bus-messages');
    const responses=typeof api==='function'?responseCache({api,onChange:()=>update(run),active:()=>!disposed&&view==='space'&&!document.hidden}):null;
    const canvasCleanup=root.MoyaiSpace?.mount(canvas,{graph:()=>({nodes:graph,active:view==='space'&&!disposed&&(!run.swarm||clock(run)?.status==='active')})});
    function setView(next,{focus=false}={}){
      if(disposed)return;view=next==='space'?'space':'chat';if(view!=='space')responses?.pause();host.hidden=view!=='space';container.classList.toggle('swarm-space-active',view==='space');
      const transcript=container.querySelector('#conversation');if(transcript)transcript.hidden=view==='space';
      for(const button of document.querySelectorAll('[data-session-view]'))button.setAttribute('aria-pressed',String(button.dataset.sessionView===view));
      onView(view);canvasCleanup?.resize?.();if(view==='space'){clockSignature='';update(run);}
      if(focus){const target=view==='space'?q('h2'):container.querySelector('#followup');target?.focus({preventScroll:true});}
    }
    function evidenceText(){
      const text=receipt(run)?.text||root.MoyaiActivity?.current(run)?.headline||'';
      return text&&run.swarm&&run.swarm.status!=='active'?`Last activity: ${text}`:text;
    }
    function tick(){
      if(disposed||host.hidden||document.hidden)return;
      responses?.poll();
      const value=clock(run),signature=JSON.stringify(value);if(signature===clockSignature)return;clockSignature=signature;
      q('.swarm-kicker').textContent=value?`SWARM${value.round?' · ROUND '+value.round:''}`:'SESSION SPACE';
      q('.swarm-state-label').textContent=value?.label||labels[run.status]||'Session';
      q('.swarm-clock').textContent=value?.timing||'';
      const reason=value?.reason||(!members(run).slice(1).length&&working.has(run.status)?'The coordinator will delegate when there is work to split.':'');
      q('.swarm-reason').textContent=reason;q('.swarm-reason').hidden=!reason;
      host.dataset.swarmStatus=value?.status||run.status;
      if(!receipt(run))q('.swarm-receipt').textContent=evidenceText();
    }
    function update(next){
      if(disposed||next.id!==run.id)return;run=next;
      const nodes=members(run),narrow=host.clientWidth<900;graph=layout(nodes,10,narrow);
      const columns=narrow?2:Math.max(1,Math.min(5,graph.length-1)),replyWidth=Math.max(100,Math.min(210,host.clientWidth/columns-24));
      graph=graph.map(node=>({...node,replyWidth}));
      // Bound background reads even when a long session has many past workers.
      const visibleIds=new Set(graph.map(node=>node.id));
      responses?.sync([...graph.slice(1),...nodes.slice(1).filter(node=>!visibleIds.has(node.id))].slice(0,50));
      host.dataset.nodeCount=String(graph.length);host.dataset.dense=String(graph.length>5);host.dataset.narrow=String(narrow);
      q('h2').textContent=titleFor(run);q('h2').title=titleFor(run);
      const html=graph.map(node=>nodeHTML(node,{selectedId,harnessName,modelName,response:node.id===run.id?{response:latestResponse(run)}:responses?.get(node.id)})).join('');
      if(html!==nodeSignature){nodeSignature=html;root.MoyaiRegions.sync(nodeHost,html);}
      const bus=busHTML(sharedResponses(nodes,node=>node.id===run.id?latestResponse(run):responses?.get(node.id)?.response),harnessName);
      if(bus!==busSignature){const atBottom=busHost.scrollHeight-busHost.scrollTop-busHost.clientHeight<32;busSignature=bus;root.MoyaiRegions.sync(busHost,bus);if(atBottom)busHost.scrollTop=busHost.scrollHeight;}
      const all=q('[data-swarm-all]');all.hidden=nodes.length<=graph.length;all.textContent=`All ${Math.max(0,nodes.length-1)} agents`;
      const latest=receipt(run),signature=JSON.stringify([latest,run.swarm?.status]);
      if(signature!==receiptSignature){receiptSignature=signature;q('.swarm-receipt').textContent=evidenceText();q('.swarm-receipt').title=q('.swarm-receipt').textContent;}
      clockSignature='';tick();
    }
    function click(event){
      const target=event.target.closest('button');if(!target||!host.contains(target))return;
      if(target.hasAttribute('data-swarm-activity')){onActivity();return;}
      if(target.hasAttribute('data-swarm-all')){onAllAgents();return;}
      if(target.dataset.swarmRetry){responses?.retry(target.dataset.swarmRetry);return;}
      const id=target.dataset.swarmAgent,node=members(run).find(item=>item.id===id);if(!node)return;
      if(node.id===run.id){setView('chat',{focus:true});return;}
      selectedId=id;update(run);onAgent(node);
    }
    host.addEventListener('click',click);
    function visibility(){if(document.hidden)responses?.pause();else if(!disposed&&view==='space')update(run);}
    if(responses)document.addEventListener('visibilitychange',visibility);
    const observer=new ResizeObserver(()=>{if(!disposed&&!host.hidden)update(run);});observer.observe(host);
    const timer=setInterval(tick,1000);
    setView(view);update(run);
    return {update,setView,tick,dispose(){disposed=true;responses?.dispose();if(responses)document.removeEventListener('visibilitychange',visibility);clearInterval(timer);observer.disconnect();canvasCleanup?.();host.removeEventListener('click',click);container.classList.remove('swarm-space-active');}};
  }
  const api={members,layout,clock,canResume,canSend,composerNote,receipt,latestResponse,responseCache,responseHTML,sharedResponses,busHTML,nodeHTML,create};root.MoyaiSwarm=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);
