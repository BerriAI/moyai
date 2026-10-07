(function(root){
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const canEdit=(message,user,role)=>!message.queue_locked&&(role==='admin'||message.user_id===user);
  function presentation(run){
    const messages=run.messages||[];
    const priority=message=>message.id===run.steer_message_id?0:message.send_immediately?1:2;
    const pending=messages.filter(message=>message.role==='user'&&message.status==='queued').sort((a,b)=>priority(a)-priority(b)||a.id-b.id);
    // Durable inputs stay queued until a worker claims them. The next input
    // belongs in the conversation if no response is ahead of it, even during
    // dispatch/startup. Match claim_message's priority and stop conditions.
    const next=messages.some(message=>message.role==='user'&&message.status==='running')||['stopping','cancelled','interrupted'].includes(run.status)?null:pending[0];
    return {queued:pending.filter(message=>message!==next&&!message.send_immediately),transcript:messages.filter(message=>(message.status!=='queued'||message===next||message.send_immediately)&&!(message.role==='assistant'&&message.status==='steered'))};
  }
  const queued=run=>presentation(run).queued;
  function card(message,run,user,role,editing,busy,attachments){
    const allowed=canEdit(message,user,role),requested=message.id===run.steer_message_id;
    const status=message.queue_locked?'Picking up…':requested?'Send requested':'Queued';
    const controls=allowed?`<div class="queued-controls"><button type="button" data-queue-action="steer" data-queue-id="${message.id}" title="Send now (Ctrl or ⌘ Enter)" ${busy||requested?'disabled':''}>↳ Send now</button><button type="button" data-queue-action="edit" data-queue-id="${message.id}" aria-label="Edit queued message" ${busy?'disabled':''}>Edit</button><button type="button" data-queue-action="delete" data-queue-id="${message.id}" aria-label="Delete queued message" title="Delete queued message" ${busy?'disabled':''}>×</button></div>`:'';
    return `<article class="queued-message" data-queued-row="${message.id}" tabindex="0" aria-label="${status} message"><div class="queued-heading"><span class="queued-mark" aria-hidden="true">↳</span><span>${status}</span><small>${message.user_id===user?'You':esc(message.user_name||'Teammate')}</small>${controls}</div>${editing?'':`<p class="queued-content">${esc(message.display_content??message.content)}</p>`}${attachments(message.attachments)}</article>`;
  }
  function create({element,runId,user,role,api,refresh,toast,sendImmediately=()=>false,attachments=()=>'',preview=()=>{},useDraft=()=>{},focusComposer=()=>{},drafts=new Map()}){
    let run={messages:[]},busy=false,signature='';
    function render(next=run){
      run=next;const messages=queued(run);
      const nextSignature=JSON.stringify([messages,run.steer_message_id,sendImmediately(),[...drafts].map(([id,draft])=>[id,draft.revision]),busy]);
      if(signature===nextSignature)return;signature=nextSignature;
      const focused=element.ownerDocument?.activeElement;
      const editingId=focused?.dataset?.queueEdit,selection=editingId?[focused.selectionStart,focused.selectionEnd]:null;
      element.hidden=!messages.length&&!drafts.size;
      element.innerHTML=`<div class="queue-heading"><span role="status">${messages.length} queued</span><small>${sendImmediately()?'New messages send immediately':'Enter to queue · Ctrl/⌘ Enter to send now'}</small></div><div class="queued-list">${messages.map(message=>card(message,run,user,role,drafts.has(message.id),busy,attachments)).join('')}${[...drafts].map(([id,draft])=>{
        const message=messages.find(message=>message.id===id),available=message&&canEdit(message,user,role);
        return `<form class="queue-editor" data-queue-editor="${id}"><label for="queue-edit-${id}">${available?'Edit queued message':'Unsent edit'}</label>${!available?'<p class="queue-edit-warning">This message has moved into the conversation. Your edit is still here; you can use it as a follow-up.</p>':message.revision!==draft.revision?'<p class="queue-edit-warning">The saved message changed. Your unsaved edit is preserved.</p>':''}<textarea id="queue-edit-${id}" data-queue-edit="${id}" maxlength="16000" required rows="3" ${busy?'disabled':''}>${esc(draft.content)}</textarea><div class="queue-edit-actions"><small>${available?'The model and attachments stay with this message.':'Your composer draft will be preserved.'}</small><button type="button" data-queue-action="discard" data-queue-id="${id}" ${busy?'disabled':''}>${available?'Cancel':'Discard edit'}</button><button type="${available?'submit':'button'}" ${available?'':`data-queue-action="followup" data-queue-id="${id}"`} ${busy?'disabled':''}>${available?'Save edit':'Use as follow-up'}</button></div></form>`;
      }).join('')}</div>`;
      element.querySelectorAll('[data-queue-action]').forEach(button=>button.onclick=()=>act(Number(button.dataset.queueId),button.dataset.queueAction));
      element.querySelectorAll('[data-queue-edit]').forEach(input=>{
        const id=Number(input.dataset.queueEdit);input.oninput=()=>{drafts.get(id).content=input.value;};
        input.onkeydown=event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)&&!event.shiftKey&&!event.isComposing){event.preventDefault();event.stopPropagation();act(id,'save-now');}};
      });
      element.querySelectorAll('[data-queue-editor]').forEach(form=>form.onsubmit=event=>{event.preventDefault();act(Number(form.dataset.queueEditor),'save');});
      element.querySelectorAll('[data-queued-row]').forEach(row=>row.onkeydown=event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)&&!event.shiftKey&&!event.isComposing){event.preventDefault();act(Number(row.dataset.queuedRow),'steer');}});
      element.querySelectorAll('[data-attachment]').forEach(button=>button.onclick=()=>preview(messages.flatMap(message=>message.attachments||[]).find(file=>file.id===button.dataset.attachment)));
      if(editingId){const input=element.querySelector(`[data-queue-edit="${editingId}"]`);input?.focus({preventScroll:true});if(input&&selection)input.setSelectionRange(...selection);}
    }
    async function act(id,action){
      if(busy)return;
      const message=run.messages.find(message=>message.id===id),draft=drafts.get(id);
      if(action==='discard'){drafts.delete(id);render();return;}
      if(action==='followup'){if(draft){useDraft(draft.content);drafts.delete(id);render();}return;}
      if(!message||message.status!=='queued'||!canEdit(message,user,role)){toast('Moyai already picked up this message, or it belongs to a teammate.');return;}
      if(action==='edit'){drafts.set(id,{content:message.content,revision:message.revision});render();element.querySelector(`[data-queue-edit="${id}"]`)?.focus();return;}
      if((action==='save'||action==='save-now')&&!draft?.content.trim())return;
      busy=true;render();
      try{
        let revision=message.revision;
        if(action==='save'||action==='save-now'){
          const saved=await api(`/api/runs/${runId}/messages/${id}`,{method:'PATCH',body:JSON.stringify({action:'edit',content:draft.content,revision:draft.revision})});
          revision=saved.revision;drafts.delete(id);
        }
        if(action==='steer'||action==='save-now'||action==='delete'){
          await api(`/api/runs/${runId}/messages/${id}`,{method:'PATCH',body:JSON.stringify({action:action==='delete'?'delete':'steer',revision})});
          if(action==='delete')drafts.delete(id);
        }
      }catch(error){toast(error.message);}
      finally{busy=false;await refresh(runId).catch(error=>toast(error.message));render();if(!drafts.has(id))focusComposer();}
    }
    return {render,act,sendFirst(){const message=queued(run).find(message=>canEdit(message,user,role));if(message)return act(message.id,'steer');toast('There is no editable queued message to send.');}};
  }
  root.MoyaiQueue={create,presentation,queued,card,canEdit};if(typeof module!=='undefined')module.exports=root.MoyaiQueue;
})(typeof globalThis!=='undefined'?globalThis:window);
