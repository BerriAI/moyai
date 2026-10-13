/* Public work history, grouped by the claimed user turn rather than enqueue time. */
(function(root){
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const settled=new Set(['idle','completed','failed','cancelled','interrupted','steered']);
  const failures=new Set(['failed','interrupted','save_failed']);
  const waiting=new Set(['reconnecting','awaiting_approval','waiting_children','waiting_credential','stopping','deleting']);
  const visible=new Set(['tool','message','status','error','plan','agents','credential','approval','artifact']);
  const labels={reconnecting:'Reconnecting to workspace',running:'Reviewing the task',provisioning:'Opening workspace',queued:'Waiting to start',saving:'Saving workspace',awaiting_approval:'Waiting for approval',waiting_children:'Waiting for agents',waiting_credential:'Waiting for access',stopping:'Stopping',deleting:'Deleting session',failed:'Response failed',cancelled:'Stopped',interrupted:'Interrupted',steered:'Earlier activity',completed:'Work finished',idle:'Work finished',save_failed:'Workspace save failed'};
  const isFocus=event=>event.kind==='status'&&event.data?.phase==='focus';
  function visibleEvents(events){
    const scoped=(data,id)=>data.turn_id&&typeof id==='string'&&id?
      JSON.stringify([String(data.turn_id),id]):'';
    const calls=new Map(),owners=new Map(),diagnostics=new Map(),delivered=new Map();
    let claimed=null;
    for(const event of events){
      const data=event.data||{};
      if(event.kind==='chat'&&event.message==='Response started'&&data.message_id)
        claimed=String(data.message_id);
      const turn=data.turn_id?String(data.turn_id):claimed;
      if(data.phase==='steering'&&data.message_id&&turn)delivered.set(turn,data.message_id);
      if(event.kind==='chat'&&event.message==='Response saved'&&String(data.message_id)===claimed)
        claimed=null;
      const key=event.kind==='tool'&&scoped(data,data.call_id);
      if(!key)continue;
      if(!calls.has(key))calls.set(key,{start:null,terminal:false,failed:false});
      const call=calls.get(key);
      if(data.phase==='started'&&data.activity_version===1&&!call.start){
        call.start=event;
        // Current and older reporters can omit input_id. Retain the input
        // delivered at the start, not the one active when a failure arrives.
        call.input=data.input_id||delivered.get(String(data.turn_id))||data.turn_id;
      }
      if(['completed','error','backgrounded'].includes(data.phase))call.terminal=true;
      if(data.phase==='error')call.failed=true;
      for(const id of [data.call_id,data.call_id.split(':')[0]]){
        const alias=scoped(data,id);
        owners.set(alias,owners.has(alias)&&owners.get(alias)!==key?null:key);
      }
    }
    const diagnosticCall=data=>owners.get(scoped(data,
      typeof data.call_id==='string'&&data.call_id?data.call_id:data.request_id));
    const confirmed=event=>event?.failure_explanation?.version===1&&
      event.failure_explanation.action?.outcome==='completed';
    for(const event of events){
      const data=event.data||{},key=diagnosticCall(data),call=calls.get(key);
      // A post-action recording failure can carry the only completion receipt.
      // A later generic diagnostic cannot make that confirmed action unknown.
      if(event.kind==='error'&&data.phase==='tool_failure'&&call?.start&&!call.terminal&&
        (!confirmed(diagnostics.get(key))||confirmed(event)))
        diagnostics.set(key,event);
    }
    // Failed operations belong in work history even when the response succeeds.
    // A standalone failure can settle an unfinished start only when its identity
    // names one call in this turn. Shared reporter prefixes are not sufficient.
    return events.flatMap(event=>{
      const data=event.data||{},key=scoped(data,data.call_id);
      if(event.kind==='tool'&&data.phase==='started'&&(calls.get(key)?.failed||diagnostics.has(key)))return [];
      if(event.kind!=='error')return [event];
      if(!data.turn_id)return [];
      if(data.phase==='tool_failure'){
        const owner=diagnosticCall(data),call=calls.get(owner);
        if(call?.failed)return [];
        if(diagnostics.has(owner)){
          if(diagnostics.get(owner)!==event)return [];
          const start=call.start.data;
          return [{...event,kind:'tool',message:call.start.message,data:{...data,
            activity_version:1,call_id:start.call_id,phase:'error',
            tool:start.tool||data.tool,category:start.category||'tool',label:start.label,
            input_id:call.input}}];
        }
        return [event];
      }
      return ['slack_attachment_read','response_completion','supervisor_failure'].includes(data.phase)||
        data.stage==='capture_sync'?[event]:[];
    });
  }
  function terminalAnswer(run){
    // The transcript moves unstarted inputs after answers. Persisted IDs retain
    // append order; negative IDs are provisional answers, not saved outcomes.
    const message=(run.messages||[]).reduce((latest,item)=>Number(item.id)>Number(latest?.id||0)?item:latest,null);
    return message?.role==='assistant'&&failures.has(message.status)?message:null;
  }
  function terminalError(run){
    if(!failures.has(run.status))return '';
    // A queued follow-up retains the previous summary until it is claimed.
    // Only a final failed answer can own a chat's detailed failure text.
    const answer=terminalAnswer(run);
    // Workspace warnings also survive turns; they cannot replace a new stop.
    if(run.chat_enabled&&!answer&&run.error)return run.error;
    return run.checkpoint_error||answer?.content||(!run.chat_enabled?run.summary:'')||
      run.error||'This task stopped before completing.';
  }
  function failureDetails(run,answer=terminalAnswer(run)){
    if(!['failed','interrupted'].includes(answer?answer.status:run.status))return '';
    if(answer?answer.role!=='assistant':run.chat_enabled)return '';
    // New answers name their input receipt explicitly. Never guess a previous
    // turn from event recency, queued inputs or the run's retained summary.
    const turn=answer?.response_to_id||(!run.chat_enabled?run.active_message_id:null);
    if(answer&&!turn)return explanationHTML(null);
    const events=(run.events||[]).filter(e=>e.kind==='error'&&e.data?.phase==='sdk_failure'&&
      (turn?String(e.data.turn_id)===String(turn):!e.data.turn_id)).sort((a,b)=>Number(a.id)-Number(b.id));
    const event=events.at(-1);
    const recovery=(run.events||[]).filter(e=>e.kind==='status'&&e.data?.phase==='recovery_decision'&&
      String(e.data.turn_id)===String(turn)&&(!event||Number(e.id)<Number(event.id)))
      .sort((a,b)=>Number(a.id)-Number(b.id)).slice(-3).map(e=>e.data.recovery);
    if(!event)return explanationHTML(null,'',recovery);
    const data=event.data,values=[];
    const add=(label,value)=>{if(value!==undefined&&value!==null&&value!=='')values.push(`<dt>${label}</dt><dd>${esc(value)}</dd>`);};
    const token=value=>typeof value==='string'&&/^[A-Za-z0-9._:/-]{1,128}$/.test(value)?value:'';
    for(const [key,label] of [['sdk','Agent'],['sdk_error','SDK category'],['code','SDK category'],['error_code','Provider code'],['stage','Stage'],
      ['native_status','SDK result'],['terminal_reason','Stop reason'],['exception_type','Exception type'],['activity_type','Activity'],['route','Endpoint'],
      ['request_id','Broker request'],['broker_request_id','Broker request'],['model_request_id','Model request']])add(label,token(data[key]));
    for(const [key,label] of [['http_status','HTTP status'],['response_status','HTTP response'],['upstream_status','Upstream status'],['pending_tools','Unresolved tools']]){
      if(Number.isInteger(data[key])&&data[key]>=0)add(key==='http_status'&&data[key]<400?'HTTP response':label,data[key]);
    }
    for(const [name,label] of [['x-moyai-model-request-id','Model request'],['x-request-id','Provider request'],['x-litellm-call-id','Gateway request']]){
      if(name!=='x-moyai-model-request-id'||!data.model_request_id)add(label,token(data.request_ids?.[name]));
    }
    return explanationHTML(event.failure_explanation,values.join(''),recovery);
  }
  const unknownExplanation={component:'Response',observed_cause:'Underlying cause unknown.',
    stage:'Unknown — stage not recorded.',action:{dispatched:null,outcome:'unknown'},retryability:'unknown',
    recovery:null,next_action:'Inspect saved receipts and the destination before deciding whether to retry.',support:{}};
  function recoveryText(value){
    const methods={catalog_read:'Tool catalog reads',startup:'Workspace startup',model_transport:'Model reconnection',context_transport:'Context reconnection',attachment_read:'Attachment read recovery'};
    const decisions={retrying:'another attempt was scheduled',recovered:'recovered',exhausted:'attempt limit reached',
      deadline:'recovery deadline reached',not_retryable:'the recorded failure was not retryable',
      unconfirmed_action:'unconfirmed tool outcomes blocked automatic replay',unknown:'stop reason not recorded',
      credential_replay_blocked:'credential acquisition may refresh a token; automatic replay is blocked',
      retry_after_unavailable:'no usable retry interval was supplied; no early retry was made',
      retry_after_budget:'the required retry interval exceeded the remaining read budget; no early retry was made'};
    if(!value||!Object.hasOwn(methods,value.method)||!Object.hasOwn(decisions,value.decision))return '';
    const count=Number.isInteger(value.attempts)&&value.attempts>=0&&value.attempts<=10000?
      `; ${value.method==='attachment_read'?'latest read-attempt index (not HTTP count)':'recorded attempts'}: ${value.attempts}`:'';
    const scheduled=Number.isInteger(value.scheduled_retries)&&value.scheduled_retries>=0&&value.scheduled_retries<=10000?`; scheduled retries: ${value.scheduled_retries}`:'';
    const limit=Number.isInteger(value.limit)&&value.limit>0&&value.limit<=10000?` (limit ${value.limit})`:'';
    return `${methods[value.method]}: ${decisions[value.decision]}${count}${scheduled}${limit}.`;
  }
  function explanationHTML(value,support='',recoveries=[],{inline=false,key=''}={}){
    const data=value?.version===1?value:unknownExplanation;
    const text=value=>typeof value==='string'?value.slice(0,2000):'Unknown.';
    const row=(label,value)=>`<dt>${label}</dt><dd>${esc(value)}</dd>`;
    const action=data.action||{};
    const outcomes={completed:'Completion receipt recorded',failed:'Failure receipt recorded',rejected:'Rejection recorded',cancelled:'Cancellation recorded',unknown:'Unknown — no confirmed outcome in this record'};
    const retries={unknown:'Unknown — no decision recorded',not_retryable:'Not retryable for the recorded condition',
      blocked_unknown_outcome:'Automatic replay blocked: outcome unknown',do_not_repeat:'Do not repeat the confirmed action',
      bounded_recovery_stopped:'The recorded attempt or time limit stopped automatic recovery',
      credential_replay_blocked:'Attachment recovery does not replay credential acquisition or token-refresh requests',
      reported_retryable:'The tool reported retry eligibility; this is not authorization to replay a write'};
    const recovery=[recoveryText(data.recovery),...recoveries.map(recoveryText)].filter(Boolean);
    const rows=row('What failed',text(data.component))+row('Observed cause',text(data.observed_cause))+
      row('Stage',text(data.stage))+row('Action dispatched',action.dispatched===true?'Yes':action.dispatched===false?'No':'Unknown — no dispatch receipt in this record')+
      row('Action outcome',outcomes[action.outcome]||outcomes.unknown)+row('Retryability',retries[data.retryability]||retries.unknown)+
      row('Recorded recovery in this turn',recovery.length?[...new Set(recovery)].join(' '):'No recovery attempt recorded. This does not prove none occurred.')+
      row('Next safe action',text(data.next_action));
    const sizes=Object.entries(data.measurements||{}).filter(([k,v])=>
      ['declared_bytes','content_length_bytes','received_bytes'].includes(k)&&Number.isSafeInteger(v)&&v>=0||
      k==='retry_after_seconds'&&Number.isFinite(v)&&v>=0&&v<=2**31-1)
      .map(([k,v])=>row(({declared_bytes:'Declared bytes',content_length_bytes:'Advertised HTTP bytes',received_bytes:'Observed bytes',retry_after_seconds:'Required retry wait (seconds)'})[k],String(v))).join('');
    const representation=data.representation||{},encodings={identity:'identity (uncompressed)',gzip:'gzip',br:'br',deflate:'deflate',zstd:'zstd',unsupported:'Unsupported encoding'};
    const knownEncoding=typeof representation.content_encoding==='string'&&Object.hasOwn(encodings,representation.content_encoding),hasPresence=typeof representation.content_encoding_present==='boolean';
    const encodingRows=knownEncoding||hasPresence?
      row('Content encoding',knownEncoding?encodings[representation.content_encoding]:'Unknown — encoding not recorded')+
      (hasPresence?row('Content-Encoding header',representation.content_encoding_present?'Present':'Absent'):''):'';
    const identifiers=Object.entries(data.support||{}).filter(([k,v])=>/^(request_id|broker_request_id|model_request_id|x-request-id|x-litellm-call-id|x-moyai-model-request-id)$/.test(k)&&typeof v==='string'&&/^[A-Za-z0-9._:-]{1,128}$/.test(v))
      .map(([k,v])=>row(esc(k.replaceAll('_',' ')),v)).join('');
    const detail=`<p>${inline?'Recorded for this operation; the final response keeps its own outcome.':'Recorded when the agent stopped. Action fields describe this record, not all work in the turn.'} Observations do not establish a root cause. Private request and response contents are omitted.</p><dl>${rows}${sizes}${encodingRows}</dl>${support||identifiers?`<details class="failure-support"${key?` data-work-key="${esc(key)}:support"`:''}><summary>Support details</summary><dl>${support}${identifiers}</dl></details>`:''}`;
    return inline?`<div class="failure-details">${detail}</div>`:`<details class="failure-details"${key?` data-work-key="${esc(key)}"`:''}><summary>Failure details</summary>${detail}</details>`;
  }
  function duration(start,end=Date.now()){
    const seconds=Math.max(0,Math.floor((Number(end)-Number(start))/1000))||0;
    return seconds<60?`${seconds}s`:seconds<3600?`${Math.floor(seconds/60)}m ${seconds%60}s`:`${Math.floor(seconds/3600)}h ${Math.floor(seconds/60)%60}m`;
  }
  function messageIndex(run){
    const messages=new Map(),steering=new Map();
    for(const message of run.messages||[]){
      messages.set(String(message.id),message);
      if(message.steering_parent_id){
        const parent=String(message.steering_parent_id);
        if(!steering.has(parent))steering.set(parent,[]);
        steering.get(parent).push(message);
      }
    }
    return {messages,steering};
  }
  function groups(run){
    const {steering}=messageIndex(run);
    const turns=new Map((run.messages||[]).filter(m=>m.role==='user'&&!m.steering_parent_id).map(m=>[String(m.id),{id:String(m.id),message:m,events:[],start:0,end:0}]));
    let current=null;const seen=new Set(),snapshots=new Map();
    for(const event of run.events||[]){
      if(event.kind==='message'&&event.data?.activity_id&&['streaming','commentary'].includes(event.data.phase))
        snapshots.set(event.data.activity_id,event);
    }
    for(let event of visibleEvents(run.events||[])){
      if(event.kind==='message'&&snapshots.has(event.data?.activity_id)){
        const latest=snapshots.get(event.data.activity_id);
        event={...latest,created_at:event.created_at};
      }
      const data=event.data||{},key=data.activity_id||event.id;
      if(key!=null&&seen.has(key))continue;if(key!=null)seen.add(key);
      if(event.kind==='chat'){
        const turn=turns.get(String(data.message_id));
        if(event.message==='Response started'&&turn){current=turn;turn.start||=Date.parse(event.created_at);}
        if(event.message==='Response received'&&turn&&data.response_complete===true){turn.responseComplete=true;turn.end||=Date.parse(event.created_at);}
        if(event.message==='Response saved'&&turn){turn.saved=true;turn.end||=Date.parse(event.created_at);if(current===turn)current=null;}
        continue;
      }
      const turn=data.turn_id?turns.get(String(data.turn_id)):current;
      if(turn&&visible.has(event.kind))turn.events.push(event);
    }
    for(const turn of turns.values()){
      // active_message_id remains on the previous response until the next claim.
      // A new request's dispatch status must never reopen completed work.
      const isCurrent=!turn.saved&&turn.message.status==='running'&&(String(run.active_message_id)===turn.id||!run.active_message_id);
      turn.status=isCurrent?(run.status||turn.message.status):turn.message.status;
      if(turn.responseComplete&&isCurrent&&turn.status==='running')turn.status='saving';
      if(turn.saved&&turn.status==='running')turn.status=String(run.active_message_id)===turn.id&&['failed','cancelled','interrupted','save_failed'].includes(run.status)?run.status:'completed';
      turn.live=!settled.has(turn.status)&&turn.status!=='save_failed'&&!!turn.start&&!turn.end;
      turn.start||=Date.parse(turn.events[0]?.created_at)||0;
      turn.end||=turn.live?0:Date.parse(turn.events.at(-1)?.created_at)||turn.start;
      // Match the server's delivered-input owner: an offered, locked child,
      // otherwise the latest receipt. Enqueue order is not delivery order.
      const receipts=turn.events.filter(event=>event.kind==='status'&&event.data?.phase==='steering'&&event.data.message_id);
      const offered=(steering.get(turn.id)||[]).find(message=>message.status==='queued'&&message.queue_locked&&
        !receipts.some(event=>String(event.data.message_id)===String(message.id)));
      turn.input=String(offered?.id||receipts.at(-1)?.data.message_id||turn.id);
      const focus=isCurrent&&run.status==='running'?turn.events.findLast(event=>isFocus(event)&&event.data.live_status===true&&event.data.activity_version===1&&
        String(event.data.turn_id)===turn.id&&String(event.data.input_id)===turn.input&&typeof event.message==='string'&&event.message.trim()):null;
      turn.rows=[];const tools=new Map();
      for(const event of turn.events){
        const data=event.data||{};
        if(isFocus(event)||data.phase==='goal'||data.phase==='recovery_decision')continue;
        if(event.kind==='tool'&&data.activity_version===1&&data.call_id){
          let row=tools.get(data.call_id);
          if(!row){row={id:data.call_id,eventId:String(event.id),kind:'tool',start:event.created_at};tools.set(data.call_id,row);turn.rows.push(row);}
          // Replayed starts cannot undo a journaled completion.
          if(row.phase&&row.phase!=='started'&&data.phase==='started')continue;
          Object.assign(row,{message:event.message,...data,failure_explanation:event.failure_explanation});
          if(data.phase!=='started')row.finishedAt=event.created_at;
        }else if(data.phase!=='processing'){
          turn.rows.push({id:String(event.id),eventId:String(event.id),kind:event.kind,message:event.message,start:event.created_at,
            failure_explanation:event.failure_explanation,
            phase:event.kind==='error'||event.kind==='tool'&&data.phase==='error'?'error':'recorded'});
        }
      }
      turn.rows=turn.rows.map(row=>({...row,state:row.phase==='started'?(turn.live&&run.activity_disconnected?'disconnected':turn.live&&!waiting.has(turn.status)?'running':turn.live?'paused':'unconfirmed'):row.phase==='error'?'error':row.phase==='completed'?'completed':row.phase==='backgrounded'?'backgrounded':'recorded'}));
      turn.headline=run.activity_disconnected&&turn.live?'Connection lost · reconnecting':
        turn.status==='running'?(focus?.message||labels.running):labels[turn.status]||'Work history';
      turn.pulse=turn.live&&!waiting.has(turn.status)&&!run.activity_disconnected;
      turn.summary=turn.live?turn.headline:'';
      turn.count=turn.rows.filter(row=>row.kind==='tool').length;
    }
    return turns;
  }
  function current(run,turns=groups(run)){
    const turn=turns.get(String(run.active_message_id));
    return turn?.start?turn:{headline:labels[run.status]||'',input:run.active_message_id,pulse:false};
  }
  function timeline(run,turns=groups(run)){
    const result=new Map(),{messages,steering}=messageIndex(run);
    for(const turn of turns.values()){
      const rows=new Map(turn.rows.map(row=>[row.eventId,row]));
      const allowedInputs=new Set([turn.id,...(steering.get(turn.id)||[]).map(m=>String(m.id)),
        ...turn.events.filter(e=>e.data?.phase==='steering'&&e.data.message_id).map(e=>String(e.data.message_id))]);
      const inputs=new Map();let delivered=turn.id;
      function input(id){
        if(!inputs.has(id)){
          const entries=[];inputs.set(id,{entries,block:null});result.set(id,entries);
        }
        return inputs.get(id);
      }
      function close(bucket,at){if(bucket.block){bucket.block.end=at;bucket.block=null;}}
      function work(bucket,id,at,key){
        if(!bucket.block){
          const block={...turn,id:`${turn.id}:${id}:${key}`,rows:[],events:[],start:at,end:0,input:id};
          bucket.entries.push({type:'work',id:block.id,block});bucket.block=block;
        }
        return bucket.block;
      }
      input(delivered);
      for(const event of turn.events){
        const data=event.data||{},at=Date.parse(event.created_at)||turn.start;
        if(isFocus(event)||data.phase==='goal'||data.phase==='recovery_decision')continue;
        if(data.phase==='steering'&&data.message_id){
          close(input(delivered),at);delivered=String(data.message_id);input(delivered);continue;
        }
        // New runtimes tag the input at emission, so delayed journal delivery
        // cannot move an earlier tool or reply past a steering receipt.
        const tagged=String(data.input_id||''),id=allowedInputs.has(tagged)?tagged:delivered;
        const bucket=input(id),row=rows.get(String(event.id));
        if(event.kind==='message'&&data.phase!=='processing'&&event.message?.trim()){
          close(bucket,at);
          bucket.entries.push({type:'update',id:String(data.activity_id||event.id),content:event.message});
        }else if(row||data.phase==='processing'){
          const block=work(bucket,id,at,event.id);block.events.push(event);
          if(row)block.rows.push(row);
        }
      }
      const latest=input(delivered);
      if((turn.live||turn.responseComplete&&!latest.entries.length)&&!latest.block)work(latest,delivered,Date.parse(messages.get(delivered)?.started_at)||turn.start,'pending');
      for(const [id,bucket] of inputs){
        for(const item of bucket.entries){
          if(item.type!=='work')continue;
          const block=item.block,isLast=id===delivered&&item===bucket.entries.at(-1);
          const active=block.rows.some(row=>['running','paused','disconnected'].includes(row.state));
          block.live=turn.live&&(isLast||active);
          block.pulse=isLast&&turn.pulse;
          block.status=isLast?turn.status:'continued';
          block.headline=isLast?turn.headline:'Earlier activity';
          block.summary=isLast?turn.summary:'';
          block.count=block.rows.filter(row=>row.kind==='tool').length;
          block.end=block.live?0:Math.max(block.end||turn.end||block.start,...block.rows.map(row=>Date.parse(row.finishedAt)||0));
          if(turn.responseComplete&&!block.live)block.end=Math.min(block.end,turn.end);
        }
      }
    }
    return result;
  }
  function updates(run,turns=groups(run)){
    return new Map([...timeline(run,turns)].map(([id,items])=>[id,items.filter(item=>item.type==='update').map(({id,content})=>({id,content}))]).filter(([,items])=>items.length));
  }
  function completedHistory(run,turns=groups(run)){
    const histories=new Map(),answered=new Set(),pending=[],{steering}=messageIndex(run);
    // The API orders messages by delivery, not enqueue time. Match each
    // answer to its delivered root input, skipping queues and steering.
    // A stack also handles an inline session-ID exchange during live work.
    for(const message of run.messages||[]){
      if(message.role==='user'){
        if(!message.steering_parent_id&&!['queued','steered'].includes(message.status)&&message.started_at!=='')pending.push(String(message.id));
      }else if(message.role==='assistant'&&message.status!=='steered'){
        const id=pending.pop();
        if(id&&message.status==='completed')answered.add(id);
      }
    }
    for(const turn of turns.values()){
      // A failed, interrupted, or waiting turn may still need a reply. Keep
      // commentary visible until its successful answer is in the transcript:
      // streamed save/idle events can arrive before the fresh message list.
      if(!['completed','idle'].includes(turn.status)||!answered.has(turn.id))continue;
      const ids=new Set([turn.id,...(steering.get(turn.id)||[]).map(message=>String(message.id)),
        ...turn.events.filter(event=>event.data?.phase==='steering'&&event.data.message_id).map(event=>String(event.data.message_id))]);
      for(const id of ids)histories.set(id,id===turn.input?(turn.start?`Worked for ${duration(turn.start,turn.end)}`:'Work history'):'Earlier activity');
    }
    return histories;
  }
  function updateHTML(update,markdown=esc){
    // Exact legacy runtime notices only; ordinary assistant prose (including
    // quotations of these notices) must keep its normal Markdown rendering.
    const compaction={
      'Compacting saved context before continuing. Completed tool receipts are preserved.':'Context compaction',
      'The agent compacted its context and is continuing. Completed tool receipts remain saved.':'Context compacted',
    };
    const label=Object.hasOwn(compaction,update.content)?compaction[update.content]:null;
    if(label)return `<details class="context-compaction" data-update-id="${esc(update.id)}"><summary><span class="context-compaction-icon" aria-hidden="true">${root.MoyaiIcon?.('list',16)||'≡'}</span><span>${label}</span><span class="context-compaction-rule" aria-hidden="true"></span><span class="context-compaction-chevron" aria-hidden="true">›</span></summary><div class="context-compaction-detail"><p>Making room to continue the conversation. Saved progress and completed tool results stay available.</p><p class="context-compaction-source">${esc(update.content)}</p></div></details>`;
    return `<article class="chat-message assistant assistant-update" data-update-id="${esc(update.id)}" aria-label="Moyai update"><div class="message-label"><img src="/static/favicon.svg?v=moyai-train-1" alt="">Moyai<small>Update</small></div><div class="message-content markdown">${markdown(update.content)}</div><button type="button" class="copy-update quiet" aria-label="Copy update" title="Copy update">${root.MoyaiIcon?.('copy',16)||'Copy'}</button></article>`;
  }
  function syncItems(slot,items,{markdown,copy}){
    const existing=new Map([...slot.children].map(node=>[node.dataset.timelineKey,node]));
    items.forEach((update,index)=>{
      const key=update.type+':'+update.id;
      let node=existing.get(key);
      if(update.type==='work'){
        if(!node){node=slot.ownerDocument.createElement('div');node.dataset.timelineKey=key;}
        syncWork(node,update.block);
      }else if(!node||node.dataset.updateContent!==update.content){
        // Only new/changed updates get new DOM. Incoming tools must not erase
        // a selected passage, focused link, or copied update the user is reading.
        const template=slot.ownerDocument.createElement('template');
        template.innerHTML=updateHTML(update,markdown);
        const fresh=template.content.firstElementChild;
        // Mount controls on the article itself; React roots cannot populate a
        // template's inert content, and this owner follows the visible update.
        MoyaiUI.render(fresh, fresh.innerHTML);
        fresh.dataset.updateContent=update.content;
        fresh.dataset.timelineKey=key;
        if(node)node.replaceWith(fresh);
        node=fresh;
        const button=node.querySelector('.copy-update');
        if(button)button.onclick=()=>copy?.(update.content,button);
        node.querySelectorAll('.copy-code').forEach(button=>button.onclick=()=>copy?.(button.closest('.code-block').querySelector('code').textContent,button));
      }
      if(slot.children[index]!==node)slot.insertBefore(node,slot.children[index]||null);
      existing.delete(key);
    });
    existing.forEach(node=>node.remove());
  }
  function rowHTML(row,turn){
    if(row.phase==='error'){
      const explanation=row.failure_explanation;
      const title=explanation?.action?.outcome==='completed'?'Recording failed after tool completion':
        `${explanation?.version===1?explanation.component:'Tool operation'} failed`;
      return `<li class="work-action error"><details data-work-key="${esc(turn.id+':'+row.id)}"><summary>
        <span class="work-icon" aria-hidden="true">!</span><span class="work-action-title">${esc(title)}</span>
        <span class="work-action-state">Details</span><span class="work-tool-chevron" aria-hidden="true">›</span></summary>
        <div class="work-action-detail">${explanationHTML(explanation,'',[],{inline:true,key:turn.id+':'+row.id})}</div></details></li>`;
    }
    const icon={command:'⌘',file:'▤'}[row.category]||(row.kind==='tool'?'◇':'·');
    const status={running:'Running',completed:'Finished',backgrounded:'Moved to background',error:'Error',paused:'Paused',unconfirmed:'No completion received',disconnected:'Reconnecting'}[row.state]||'';
    const detail=row.command||row.path;
    const title=row.path?`${row.message} · ${row.path}`:row.command?`${row.message} · ${row.command.split('\n')[0].slice(0,110)}`:row.message;
    const timer=row.duration_ms!=null?duration(0,row.duration_ms):row.state==='running'?`<span data-work-timer="${Date.parse(row.start)}">${duration(Date.parse(row.start))}</span>`:'';
    const line=`<span class="work-icon" aria-hidden="true">${icon}</span><span class="work-action-title">${esc(title)}</span><span class="work-action-state">${esc(status)}</span><span class="work-action-time">${timer}</span>`;
    const payload=['input','output'].filter(key=>typeof row[key]==='string').map(key=>`<span>${key==='input'?'Input':'Result'}</span><pre>${esc(row[key])}</pre>`).join('');
    const thumbnail=typeof row.image_preview==='string'&&row.image_preview.length<=66000&&/^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/.test(row.image_preview)?`<img src="${esc(row.image_preview)}" alt="Viewed image" loading="lazy">`:'';
    const preview=row.category==='image'&&typeof row.image_path==='string'?`<p class="work-image"><a data-file-ref="${esc(row.image_path)}" data-file-image="true" data-file-label="Viewed image" aria-disabled="true">${thumbnail||'Viewed image'}</a></p>`:'';
    const content=`${detail?`<span>${row.command?'Command':'File'}</span><pre>${esc(detail)}</pre>`:''}${preview}${payload}${row.details_notice?`<small>${esc(row.details_notice)}</small>`:''}${row.image_notice?`<small>${esc(row.image_notice)}</small>`:''}${row.exit_code!=null?`<small>Exit code ${esc(row.exit_code)}</small>`:''}`;
    const fallback=row.phase==='started'?'Waiting for tool results.':'Inputs and results were not recorded for this activity.';
    return `<li class="work-action ${esc(row.state)}">${row.kind==='tool'||detail?`<details data-work-key="${esc(turn.id+':'+row.id)}"><summary>${line}<span class="work-tool-chevron" aria-hidden="true">›</span></summary><div class="work-action-detail">${content||`<small>${fallback}</small>`}</div></details>`:`<div class="work-action-line">${line}</div>`}</li>`;
  }
  function html(turn){
    if(!turn||(!turn.start&&!turn.rows.length))return '';
    const key='turn:'+turn.id;
    // Commentary stays separate from individual tool blocks. Completed
    // conversation history wraps both at the input level in sync().
    const rows=turn.rows.filter(row=>row.kind!=='message');
    const older=rows.slice(0,-7),recent=rows.slice(-7);
    return `<details class="turn-work ${turn.pulse?'is-live':''}" data-work-key="${esc(key)}" data-turn="${esc(turn.id)}">
      <summary class="work-heading"><span class="work-chevron" aria-hidden="true">›</span><span class="work-indicator" aria-hidden="true">${turn.live?'':turn.status==='completed'||turn.status==='idle'?'✓':['failed','cancelled','interrupted','save_failed'].includes(turn.status)?'!':''}</span><span class="work-title">${!turn.live&&(['completed','idle'].includes(turn.status)||turn.responseComplete&&turn.status==='saving')&&turn.start?`<span class="sr-only">${esc(turn.headline)} · </span>Worked for ${duration(turn.start,turn.end||Date.now())}`:esc(turn.headline)}</span><span class="work-count">${turn.count?`${turn.count} action${turn.count===1?'':'s'}`:''}</span><time class="work-elapsed" ${turn.live?`data-work-timer="${turn.start}"`:''}>${duration(turn.start,turn.end||Date.now())}</time></summary>
      <div class="work-body">${older.length?`<details class="work-earlier" data-work-key="earlier:${esc(turn.id)}"><summary>Show ${older.length} earlier updates</summary><ol class="work-list">${older.map(row=>rowHTML(row,turn)).join('')}</ol></details>`:''}
      <ol class="work-list">${recent.map(row=>rowHTML(row,turn)).join('')}</ol>
      ${turn.live?`<div class="work-current" role="status"><span class="work-live-dot" aria-hidden="true"></span><span>${esc(waiting.has(turn.status)?turn.headline:turn.summary||turn.headline)}</span></div>`:''}</div></details>`;
  }
  function tick(container){container?.querySelectorAll('[data-work-timer]').forEach(node=>{node.textContent=duration(Number(node.dataset.workTimer));});}
  function syncWork(slot,turn){
    const signature=JSON.stringify(turn);
    if(slot.dataset.workSignature===signature)return;
    slot.dataset.workSignature=signature;
    const justFinished=slot.dataset.workLive==='true'&&!turn?.live;
    slot.dataset.workLive=String(!!turn?.live);
    const previous=new Map([...slot.querySelectorAll('[data-work-key]')].map(node=>[node.dataset.workKey,{open:node.open,manual:node.dataset.workManual}]));
    const focused=slot.contains(slot.ownerDocument?.activeElement)?slot.ownerDocument.activeElement.closest('[data-work-key]')?.dataset.workKey:null;
    MoyaiUI.render(slot, html(turn));
    slot.querySelectorAll('[data-work-key]').forEach(node=>{
      const before=previous.get(node.dataset.workKey);
      if(before&&!(justFinished&&node.dataset.workKey.startsWith('turn:')&&!before.manual))node.open=before.open;
      if(before?.manual)node.dataset.workManual=before.manual;
      node.querySelector('summary')?.addEventListener('click',()=>{node.dataset.workManual=node.open?'closed':'open';});
      if(focused===node.dataset.workKey)node.querySelector('summary')?.focus({preventScroll:true});
    });
  }
  function sync(container,run,options={}){
    if(!container)return;
    const nearBottom=container.scrollHeight-container.scrollTop-container.clientHeight<100;
    const turns=groups(run),byInput=timeline(run,turns),histories=completedHistory(run,turns),{messages}=messageIndex(run);
    const deferredTurns=new Set(run.deferred_activity||[]),loadedTurns=new Set(run.loaded_activity||[]);
    container.querySelectorAll('[data-activity-slot]').forEach(slot=>{
      const message=messages.get(slot.dataset.activitySlot);
      const turnId=String(message?.steering_parent_id||slot.dataset.activitySlot);
      const deferred=options.loadActivity&&deferredTurns.has(turnId)&&!loadedTurns.has(turnId);
      let placeholder=slot.querySelector?.(':scope > [data-deferred-activity]');
      if(deferred){
        if(!placeholder){
          placeholder=slot.ownerDocument.createElement('details');placeholder.className='turn-work';
          placeholder.dataset.deferredActivity=turnId;
          placeholder.innerHTML='<summary class="work-heading"><span class="work-chevron" aria-hidden="true">›</span><span class="work-title"></span></summary><div class="work-body" role="status"></div>';
          slot.replaceChildren(placeholder);
          placeholder.addEventListener('toggle',async()=>{
            if(!placeholder.open||placeholder.dataset.loading==='true')return;
            placeholder.dataset.loading='true';const body=placeholder.querySelector('.work-body');body.textContent='Loading work history…';
            try{await options.loadActivity(turnId);}catch{
              body.textContent='Could not load work history. Close and reopen to retry.';
            }finally{delete placeholder.dataset.loading;}
          });
        }
        const turn=turns.get(turnId);
        placeholder.querySelector('.work-title').textContent=message?.steering_parent_id?'Earlier activity':turn?.start?`Worked for ${duration(turn.start,turn.end||turn.start)}`:'Work history';
        return;
      }
      const expand=placeholder?.open,focused=placeholder?.contains(slot.ownerDocument.activeElement);placeholder?.remove();
      const items=byInput.get(slot.dataset.activitySlot)||[];
      const title=items.some(item=>item.type==='update')?histories.get(slot.dataset.activitySlot):null;
      let history=slot.querySelector?.(':scope > .completed-work');
      if(title){
        if(!history){
          history=slot.ownerDocument.createElement('details');history.className='turn-work completed-work';
          history.innerHTML='<summary class="work-heading"><span class="work-chevron" aria-hidden="true">›</span><span class="work-title"></span></summary><div class="work-body"></div>';
          const body=history.querySelector('.work-body');
          const focused=slot.contains(slot.ownerDocument.activeElement);
          // Move the existing nodes so links, copy controls, and manually
          // expanded tool details survive the transition to saved history.
          body.append(...slot.children);slot.append(history);
          if(focused)history.querySelector('summary').focus({preventScroll:true});
        }
        history.querySelector('.work-title').textContent=title;
        syncItems(history.querySelector('.work-body'),items,options);
      }else{
        if(history){slot.append(...history.querySelector('.work-body').children);history.remove();}
        syncItems(slot,items,options);
      }
      if(expand){
        const details=slot.querySelector('details');if(details){details.open=true;details.dataset.workManual='open';}
        else if(!slot.children.length)slot.textContent='No recorded work history.';
      }
      if(focused)slot.querySelector('summary')?.focus({preventScroll:true});
    });
    if(nearBottom)container.scrollTop=container.scrollHeight;
  }
  const api={groups,current,isFocus,visibleEvents,terminalAnswer,terminalError,failureDetails,explanationHTML,timeline,updates,completedHistory,updateHTML,html,duration,tick,sync,syncWork};root.MoyaiActivity=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);
