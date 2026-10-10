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
  function layout(nodes,limit=10){
    if(!nodes.length)return [];
    // Keep active/attention nodes visible; the existing Subagents panel exposes
    // the complete hierarchy when a large team exceeds the visual canvas.
    const children=nodes.slice(1).map((node,index)=>({node,index})).sort((a,b)=>
      Number(attention.has(b.node.status))-Number(attention.has(a.node.status))||
      Number(working.has(b.node.status))-Number(working.has(a.node.status))||a.index-b.index).slice(0,limit).map(item=>item.node);
    const positions=children.length===1?[[.5,.75]]:children.length===2?[[.27,.72],[.73,.72]]:children.length===3?[[.18,.64],[.5,.79],[.82,.64]]:children.length===4?[[.17,.59],[.36,.77],[.64,.77],[.83,.59]]:children.map((_,index)=>{
      const row=index<5?0:1,count=Math.min(5,children.length-row*5),column=index-row*5;
      return [count===1?.5:(.12+(.76/(count-1))*column),row===0?.63:.81];
    });
    return [{...nodes[0],x:.5,y:.40},...children.map((node,index)=>({...node,x:positions[index][0],y:Math.min(positions[index][1],limit<=4?.72:.81)}))];
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
  function marker(status){
    if(['idle','completed'].includes(status))return '<path d="m3 6 2 2 4-4"/>';
    if(attention.has(status))return '<path d="M6 2.5v4M6 9v.1"/>';
    if(['cancelled','stopping','deleting'].includes(status))return '<rect x="3.5" y="3.5" width="5" height="5" rx=".5"/>';
    return '<circle cx="6" cy="6" r="2.5"/>';
  }
  function nodeHTML(node,{selectedId,harnessName=id=>id||'Harness unavailable',modelName=id=>id||'Model unavailable'}={}){
    const logo=root.MoyaiProviderLogos?.harness(node.harness);
    const status=labels[node.status]||'Status unavailable';
    const harness=harnessName(node.harness),model=modelName(node.active_model||node.model);
    const title=[node.label,harness,model,status,node.prompt||node.task||''].filter(Boolean).join(' · ');
    return `<div class="swarm-node-position" data-region-key="node:${esc(node.id)}" style="left:${node.x*100}%;top:${node.y*100}%"><div data-region-key="control:${esc(node.id)}" data-region-leaf><button type="button" class="quiet swarm-node" data-swarm-agent="${esc(node.id)}" data-status="${esc(node.status)}" aria-pressed="${selectedId===node.id}" aria-label="${esc(title+'. Open conversation and activity.')}" title="${esc(title)}"><span class="swarm-node-mark">${logo?`<img src="${esc(logo)}" alt="" width="38" height="38">`:`<span class="swarm-node-fallback" aria-hidden="true">${esc(String(node.label).slice(0,1))}</span>`}<span class="swarm-node-state" aria-hidden="true"><svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round">${marker(node.status)}</svg></span></span><span class="swarm-node-label">${esc(node.label)}</span><span class="swarm-node-status">${esc(status)}</span></button></div></div>`;
  }
  function create({host,layout:container,run:initial,initialView,onView=()=>{},onAgent=()=>{},onActivity=()=>{},onAllAgents=()=>{},titleFor=run=>run.display_title||run.prompt||'Session',harnessName,modelName}){
    let run=initial,disposed=false,selectedId='',view=initialView||(run.swarm?'space':'chat'),graph=[],nodeSignature='',receiptSignature='',clockSignature='';
    root.MoyaiUI.render(host, `<canvas class="swarm-canvas" aria-hidden="true"></canvas><header class="swarm-mission"><p class="swarm-kicker"></p><h2 tabindex="-1"></h2><p class="swarm-state-line"><span class="swarm-state-label"></span><time class="swarm-clock"></time></p><p class="swarm-reason" hidden></p></header><div class="swarm-nodes" role="group" aria-label="Session agents. Lines show delegation; signals show active workers."></div><div class="swarm-evidence"><p class="swarm-receipt" aria-live="polite"></p><div class="swarm-evidence-actions"><button type="button" class="quiet" data-swarm-activity>View activity</button><button type="button" class="quiet" data-swarm-all hidden></button></div></div>`);
    const q=selector=>host.querySelector(selector);
    const canvas=q('canvas'),nodeHost=q('.swarm-nodes');
    const canvasCleanup=root.MoyaiSpace?.mount(canvas,{graph:()=>({nodes:graph,active:view==='space'&&!disposed&&(!run.swarm||clock(run)?.status==='active')})});
    function setView(next,{focus=false}={}){
      if(disposed)return;view=next==='space'?'space':'chat';host.hidden=view!=='space';container.classList.toggle('swarm-space-active',view==='space');
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
      if(disposed||host.hidden)return;
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
      const nodes=members(run);graph=layout(nodes,host.clientWidth<640?4:10);
      q('h2').textContent=titleFor(run);q('h2').title=titleFor(run);
      const html=graph.map(node=>nodeHTML(node,{selectedId,harnessName,modelName})).join('');
      if(html!==nodeSignature){nodeSignature=html;root.MoyaiRegions.sync(nodeHost,html);}
      const all=q('[data-swarm-all]');all.hidden=nodes.length<=graph.length;all.textContent=`All ${Math.max(0,nodes.length-1)} agents`;
      const latest=receipt(run),signature=JSON.stringify([latest,run.swarm?.status]);
      if(signature!==receiptSignature){receiptSignature=signature;q('.swarm-receipt').textContent=evidenceText();q('.swarm-receipt').title=q('.swarm-receipt').textContent;}
      clockSignature='';tick();
    }
    function click(event){
      const target=event.target.closest('button');if(!target||!host.contains(target))return;
      if(target.hasAttribute('data-swarm-activity')){onActivity();return;}
      if(target.hasAttribute('data-swarm-all')){onAllAgents();return;}
      const id=target.dataset.swarmAgent,node=members(run).find(item=>item.id===id);if(!node)return;
      if(node.id===run.id){setView('chat',{focus:true});return;}
      selectedId=id;update(run);onAgent(node);
    }
    host.addEventListener('click',click);
    const observer=new ResizeObserver(()=>{if(!disposed&&!host.hidden)update(run);});observer.observe(host);
    const timer=setInterval(tick,1000);
    setView(view);update(run);
    return {update,setView,tick,dispose(){disposed=true;clearInterval(timer);observer.disconnect();canvasCleanup?.();host.removeEventListener('click',click);container.classList.remove('swarm-space-active');}};
  }
  const api={members,layout,clock,canResume,canSend,composerNote,receipt,nodeHTML,create};root.MoyaiSwarm=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);
