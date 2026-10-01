/* Public work history, grouped by the claimed user turn rather than enqueue time. */
(function(root){
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const settled=new Set(['idle','completed','failed','cancelled','interrupted','steered']);
  const waiting=new Set(['reconnecting','awaiting_approval','waiting_children','waiting_credential']);
  const visible=new Set(['tool','message','status','error','plan','agents','credential','approval','artifact']);
  const labels={reconnecting:'Reconnecting to workspace',running:'Working',provisioning:'Opening workspace',queued:'Waiting to start',saving:'Saving workspace',awaiting_approval:'Waiting for approval',waiting_children:'Waiting for agents',waiting_credential:'Waiting for a key',stopping:'Stopping',failed:'Response failed',cancelled:'Stopped',interrupted:'Interrupted',steered:'Paused for your message',completed:'Work finished',idle:'Work finished',save_failed:'Workspace save failed'};
  function duration(start,end=Date.now()){
    const seconds=Math.max(0,Math.floor((Number(end)-Number(start))/1000))||0;
    return seconds<60?`${seconds}s`:seconds<3600?`${Math.floor(seconds/60)}m ${seconds%60}s`:`${Math.floor(seconds/3600)}h ${Math.floor(seconds/60)%60}m`;
  }
  function groups(run){
    const turns=new Map((run.messages||[]).filter(m=>m.role==='user'&&!m.steering_parent_id).map(m=>[String(m.id),{id:String(m.id),message:m,events:[],start:0,end:0}]));
    let current=null;const seen=new Set();
    for(const event of run.events||[]){
      const data=event.data||{},key=data.activity_id||event.id;
      if(key!=null&&seen.has(key))continue;if(key!=null)seen.add(key);
      if(event.kind==='chat'){
        const turn=turns.get(String(data.message_id));
        if(event.message==='Response started'&&turn){current=turn;turn.start||=Date.parse(event.created_at);}
        if(event.message==='Response saved'&&turn){turn.end=Date.parse(event.created_at);if(current===turn)current=null;}
        continue;
      }
      const turn=data.turn_id?turns.get(String(data.turn_id)):current;
      if(turn&&visible.has(event.kind))turn.events.push(event);
    }
    for(const turn of turns.values()){
      const isCurrent=String(run.active_message_id)===turn.id||(!run.active_message_id&&turn.message.status==='running');
      turn.status=isCurrent&&!settled.has(run.status)?run.status:turn.message.status;
      if(isCurrent&&settled.has(run.status)&&turn.status==='running')turn.status=run.status;
      if(turn.end&&turn.status==='running')turn.status='completed';
      turn.live=!settled.has(turn.status)&&turn.status!=='save_failed'&&!!turn.start&&!turn.end;
      turn.start||=Date.parse(turn.events[0]?.created_at)||0;
      turn.end||=turn.live?0:Date.parse(turn.events.at(-1)?.created_at)||turn.start;
      turn.rows=[];const tools=new Map();
      for(const event of turn.events){
        const data=event.data||{};
        if(event.kind==='tool'&&data.activity_version===1&&data.call_id){
          let row=tools.get(data.call_id);
          if(!row){row={id:data.call_id,kind:'tool',start:event.created_at};tools.set(data.call_id,row);turn.rows.push(row);}
          // Replayed starts cannot undo a journaled completion.
          if(row.phase&&row.phase!=='started'&&data.phase==='started')continue;
          Object.assign(row,{message:event.message,...data});
        }else if(data.phase!=='processing'){
          turn.rows.push({id:String(event.id),kind:event.kind,message:event.message,start:event.created_at,
            phase:event.kind==='error'?'error':'recorded'});
        }
      }
      turn.rows=turn.rows.map(row=>({...row,state:row.phase==='started'?(turn.live&&run.activity_disconnected?'disconnected':turn.live&&!waiting.has(turn.status)?'running':turn.live?'paused':'unconfirmed'):row.phase==='error'?'error':row.phase==='completed'?'completed':row.phase==='backgrounded'?'backgrounded':'recorded'}));
      const latest=turn.events.at(-1),running=turn.rows.filter(row=>row.state==='running');
      turn.headline=run.activity_disconnected&&turn.live?'Connection lost · reconnecting':labels[turn.status]||'Work history';
      turn.pulse=turn.live&&!waiting.has(turn.status)&&!run.activity_disconnected;
      turn.summary=running.length?`${running.at(-1).message}${running.length>1?` · ${running.length} tools running`:''}`:
        turn.live&&latest?.data?.phase==='processing'?'Reviewing results and preparing the next step':
        turn.live&&latest?.kind==='message'?latest.message:turn.live&&latest?latest.message:'';
      turn.count=turn.rows.filter(row=>row.kind==='tool').length;
    }
    return turns;
  }
  function rowHTML(row,turn){
    const icon={command:'⌘',file:'▤'}[row.category]||(row.kind==='message'?'✦':row.kind==='tool'?'◇':'·');
    const status={running:'Running',completed:'Finished',backgrounded:'Moved to background',error:'Error',paused:'Paused',unconfirmed:'No completion received',disconnected:'Reconnecting'}[row.state]||'';
    const detail=row.command||row.path;
    const title=row.path?`${row.message} · ${row.path}`:row.command?`${row.message} · ${row.command.split('\n')[0].slice(0,110)}`:row.message;
    const timer=row.duration_ms!=null?duration(0,row.duration_ms):row.state==='running'?`<span data-work-timer="${Date.parse(row.start)}">${duration(Date.parse(row.start))}</span>`:'';
    const line=`<span class="work-icon" aria-hidden="true">${icon}</span><span class="work-action-title">${esc(title)}</span><span class="work-action-state">${esc(status)}</span><span class="work-action-time">${timer}</span>`;
    if(row.kind==='message')return `<li class="work-commentary"><span aria-hidden="true">✦</span><p>${esc(row.message)}</p></li>`;
    return `<li class="work-action ${esc(row.state)}">${detail?`<details data-work-key="${esc(turn.id+':'+row.id)}"><summary>${line}</summary><div class="work-action-detail"><span>${row.command?'Command':'File'}</span><pre>${esc(detail)}</pre>${row.exit_code!=null?`<small>Exit code ${esc(row.exit_code)}</small>`:''}</div></details>`:`<div class="work-action-line">${line}</div>`}</li>`;
  }
  function html(turn){
    if(!turn||(!turn.start&&!turn.rows.length))return '';
    const key='turn:'+turn.id;
    const older=turn.rows.slice(0,-7),recent=turn.rows.slice(-7);
    return `<details class="turn-work ${turn.pulse?'is-live':''}" data-work-key="${esc(key)}" data-turn="${esc(turn.id)}" ${turn.live?'open':''}>
      <summary class="work-heading"><span class="work-indicator" aria-hidden="true">${turn.live?'':turn.status==='completed'||turn.status==='idle'?'✓':'!'}</span><span class="work-title">${esc(turn.headline)}</span><span class="work-count">${turn.count?`${turn.count} action${turn.count===1?'':'s'}`:''}</span><time class="work-elapsed" ${turn.live?`data-work-timer="${turn.start}"`:''}>${duration(turn.start,turn.end||Date.now())}</time><span class="work-chevron" aria-hidden="true">›</span></summary>
      <div class="work-body">${older.length?`<details class="work-earlier" data-work-key="earlier:${esc(turn.id)}"><summary>Show ${older.length} earlier updates</summary><ol class="work-list">${older.map(row=>rowHTML(row,turn)).join('')}</ol></details>`:''}
      <ol class="work-list">${recent.map(row=>rowHTML(row,turn)).join('')}</ol>
      ${turn.live?`<div class="work-current" role="status"><span class="work-live-dot" aria-hidden="true"></span><span>${esc(waiting.has(turn.status)?turn.headline:turn.summary||turn.headline)}</span></div>`:''}</div></details>`;
  }
  function tick(container){container?.querySelectorAll('[data-work-timer]').forEach(node=>{node.textContent=duration(Number(node.dataset.workTimer));});}
  function sync(container,run){
    if(!container)return;
    const nearBottom=container.scrollHeight-container.scrollTop-container.clientHeight<100;
    const turns=groups(run);
    container.querySelectorAll('[data-work-slot]').forEach(slot=>{
      const turn=turns.get(slot.dataset.workSlot);
      const signature=JSON.stringify(turn);
      if(slot.dataset.workSignature===signature)return;
      slot.dataset.workSignature=signature;
      const justFinished=slot.dataset.workLive==='true'&&!turn?.live;
      slot.dataset.workLive=String(!!turn?.live);
      const previous=new Map([...slot.querySelectorAll('[data-work-key]')].map(node=>[node.dataset.workKey,{open:node.open,manual:node.dataset.workManual}]));
      const focused=slot.contains(container.ownerDocument?.activeElement)?container.ownerDocument.activeElement.closest('[data-work-key]')?.dataset.workKey:null;
      slot.innerHTML=html(turn);
      slot.querySelectorAll('[data-work-key]').forEach(node=>{
        const before=previous.get(node.dataset.workKey);
        if(before&&!(justFinished&&node.dataset.workKey.startsWith('turn:')&&!before.manual))node.open=before.open;
        if(before?.manual)node.dataset.workManual=before.manual;
        node.querySelector('summary')?.addEventListener('click',()=>{node.dataset.workManual=node.open?'closed':'open';});
        if(focused===node.dataset.workKey)node.querySelector('summary')?.focus({preventScroll:true});
      });
    });
    if(nearBottom)container.scrollTop=container.scrollHeight;
  }
  const api={groups,html,duration,tick,sync};root.MoyaiActivity=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);
