const $ = (s) => document.querySelector(s);
const esc = (s = '') => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {view:'tasks', runs:[], config:{missing:[]}, connections:[], organization:{}, role:'member', selected:null, source:null, csrf:'', drafts:{}, modelDrafts:{}, pendingMessages:{}, pageVersion:0, newDraft:{}, detailsOpen:false, sending:new Set()};
const terminal = new Set(['completed','failed','cancelled','interrupted','idle']);
const providerNames = {linear:'Linear', slack:'Slack', notion:'Notion', github:'GitHub'};
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers:{'Content-Type':'application/json', 'X-CSRF-Token':state.csrf, ...options.headers}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'The request could not be completed.');
  return body;
}
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(state.toast); state.toast = setTimeout(() => $('#toast').hidden = true, 5500); }
function statusLabel(status) { return `<span class="status ${esc(status)}">${esc(status==='idle'?'Ready':status.replaceAll('_',' '))}</span>`; }
function relative(date) { const min = Math.max(0, Math.floor((Date.now() - new Date(date)) / 60000)); return min < 1 ? 'Just now' : min < 60 ? `${min}m ago` : min < 1440 ? `${Math.floor(min/60)}h ago` : new Date(date).toLocaleDateString(); }
function stopStream(){ clearTimeout(state.streamRetry); state.streamRetry=null; state.source?.close(); state.source = null; }
function sessionTitle(run){return run.prompt.split('\n')[0].replace(/\s+/g,' ').trim();}
function modelName(model=state.config.model){return (state.config.models||[]).find(m=>m.id===model)?.name||model||'Hermes Agent';}
function modelPicker(id,selected,disabled=false){return `<label class="model-picker"><span class="sr-only">Model for next message</span><select id="${id}" aria-label="Model for next message" ${disabled?'disabled':''}>${(state.config.models||[]).map(m=>`<option value="${esc(m.id)}" ${m.id===selected?'selected':''}>${esc(m.name)}</option>`).join('')}</select></label>`;}

function setSidebar(open){document.body.classList.toggle('sidebar-open',open);$('#sidebar-scrim').hidden=!open;$('#open-sidebar').setAttribute('aria-expanded',String(open));$('#sidebar').inert=matchMedia('(max-width:850px)').matches&&!open;if(open)$('#session-search').focus();}
function renderSidebar(){
  const search=($('#session-search').value||'').toLowerCase();
  const runs=state.runs.filter(r=>r.prompt.toLowerCase().includes(search));
  $('#task-count').textContent=state.runs.length;
  $('#workspace-name').textContent=state.organization.name||'Workspace';
  $('.avatar').textContent=(state.organization.name||'W')[0];
  const signature=JSON.stringify([state.selected,search,runs.map(r=>[r.id,r.prompt,r.status,r.mode,r.updated_at,r.created_at])]);
  if(state.sidebarSignature===signature){
    document.querySelectorAll('[data-session-time]').forEach(label=>{const r=runs.find(r=>r.id===label.dataset.sessionTime);if(r)label.textContent=relative(r.updated_at||r.created_at)+(r.mode==='demo'?' · Demo':'');});
    return;
  }
  state.sidebarSignature=signature;
  $('#session-list').innerHTML=runs.length?runs.map(r=>`<button class="session-link ${state.selected===r.id?'selected':''}" data-run="${r.id}" ${state.selected===r.id?'aria-current="page"':''} title="${esc(sessionTitle(r))}"><span class="session-dot ${esc(r.status)}" aria-label="${esc(r.status==='idle'?'Ready':r.status)}"></span><span class="session-link-body"><span class="session-link-title">${esc(sessionTitle(r))}</span><small data-session-time="${r.id}">${relative(r.updated_at||r.created_at)}${r.mode==='demo'?' · Demo':''}</small></span></button>`).join(''):`<p class="sidebar-empty">${search?'No matching sessions.':'Your conversations will appear here.'}</p>`;
}
function setView(view,title){
  document.body.classList.toggle('chat-view',view==='chat');
  document.body.classList.toggle('home-view',view==='tasks');
  document.querySelectorAll('.nav-button').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  $('#page-title').textContent=title;
  $('#header-actions').innerHTML='<span class="environment">Your team’s agent</span>';
  setSidebar(false);renderSidebar();
}
function autoSize(input){input.style.height='auto';input.style.height=Math.min(input.scrollHeight,200)+'px';}
function bindComposer(input,form){
  input.addEventListener('input',()=>autoSize(input));
  input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!form.querySelector('[type="submit"]').disabled)form.requestSubmit();}});
  autoSize(input);
}
async function navigate(view) {
  stopStream();state.pageVersion++;state.view=view;state.selected=null;
  setView(view,{tasks:'New session',connections:'Connections',runtime:'Runtime',spend:'Spend'}[view]);
  history.replaceState(null,'',view==='tasks'?'#tasks':'#'+view);
  if(view==='tasks')await renderHome();else if(view==='connections')await renderConnections();else if(view==='spend')await renderSpend();else await renderRuntime();
}
async function refreshRuns(){state.runs=await api('/api/runs');renderSidebar();}
async function renderHome(){
  const version=state.pageVersion;
  const [,connections]=await Promise.all([refreshRuns(),api('/api/connections')]);
  if(version!==state.pageVersion)return;state.connections=connections;
  const apps=connections.filter(c=>c.connected&&c.enabled),draft=state.newDraft;
  $('#content').innerHTML=`<section class="new-conversation"><div class="welcome-mark"><img src="/static/favicon.svg?v=agent-2" alt=""><span>Moyai Devin</span></div><h1>What are we working on?</h1><p class="welcome-note">A teammate for your code, questions, and next steps.</p>
    <form id="task-form" class="composer"><textarea id="prompt" name="prompt" aria-label="Message Moyai Devin" placeholder="Ask Moyai to build, investigate, or pick up a thread…" required minlength="3" maxlength="16000" rows="3"></textarea><div class="composer-toolbar"><details class="task-options"><summary aria-label="Session options"><span aria-hidden="true">＋</span> Context & tools</summary><div class="task-settings"><div class="field"><label for="repo">GitHub repository</label><input id="repo" type="url" placeholder="https://github.com/owner/repo" value="${esc(draft.repo||'')}"></div><div class="field"><label for="mode">Execution</label><select id="mode"><option value="modal" ${state.config.cloud_ready?'':'disabled'}>Cloud session</option><option value="demo" ${state.config.cloud_ready?'':'selected'}>Demo · simulated</option></select></div><div id="plugin-options"><span>Organization connections</span>${apps.map(c=>`<label class="plugin-toggle"><input type="checkbox" name="plugin" value="${c.id}" ${!draft.plugins||draft.plugins.includes(c.id)?'checked':''}>${providerNames[c.id]}</label>`).join('')||'<small>Connect apps in organization settings.</small>'}</div></div></details>${modelPicker('new-model',draft.model||state.config.model)}<button class="send-button" type="submit" aria-label="Start session" title="Start session"><span aria-hidden="true">↑</span></button></div></form>
    <div class="composer-caption"><span id="mode-note">${state.config.cloud_ready?'Your own cloud workspace':'Demo mode · no AI or cloud usage'}</span><span>Enter to send · Shift + Enter for a new line</span></div>
    <div class="suggestions"><button type="button" data-prompt="Help me investigate a bug. "><span aria-hidden="true">⌘</span> Investigate a bug</button><button type="button" data-prompt="Read the repository and explain how it works. Make no changes. "><span aria-hidden="true">⌑</span> Explore a codebase</button><button type="button" data-prompt="Find the team context for "><span aria-hidden="true">⌕</span> Find team context</button></div><p class="connected-note">${apps.length?`<span class="connected-dot"></span>${apps.map(c=>providerNames[c.id]).join(', ')} connected`:'Add your team’s apps in Connections'}</p></section>`;
  $('#prompt').value=draft.prompt||'';
  if(draft.mode&&($('#mode option[value="'+draft.mode+'"]').disabled===false))$('#mode').value=draft.mode;
  const saveDraft=()=>{state.newDraft={prompt:$('#prompt').value,repo:$('#repo').value,mode:$('#mode').value,model:$('#new-model').value,plugins:[...document.querySelectorAll('[name="plugin"]:checked')].map(x=>x.value)};};
  $('#task-form').oninput=saveDraft;$('#task-form').onchange=saveDraft;$('#task-form').onsubmit=submitTask;
  $('#mode').addEventListener('change',()=>{$('#new-model').disabled=$('#mode').value==='demo';$('#mode-note').textContent=$('#mode').value==='demo'?'Demo mode · no AI or cloud usage':'Your own cloud workspace';});
  $('#mode').dispatchEvent(new Event('change'));
  bindComposer($('#prompt'),$('#task-form'));
  document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('#prompt').value=b.dataset.prompt;saveDraft();autoSize($('#prompt'));$('#prompt').focus();});
}
async function submitTask(e){
  e.preventDefault();const button=$('#task-form button[type="submit"]'); button.disabled=true;
  try{ const run=await api('/api/runs',{method:'POST',body:JSON.stringify({prompt:$('#prompt').value,repo_url:$('#repo').value,mode:$('#mode').value,model:$('#new-model').value,plugins:[...document.querySelectorAll('[name="plugin"]:checked')].map(x=>x.value)})});state.newDraft={};await refreshRuns();await openRun(run.id); }
  catch(error){toast(error.message);button.disabled=false;}
}
async function openRun(id){
  stopStream();const version=++state.pageVersion;state.selected=id;const run=await api(`/api/runs/${id}`);if(version!==state.pageVersion)return;state.view='tasks';setView(run.chat_enabled?'chat':'legacy',sessionTitle(run));history.replaceState(null,'','#run='+id);
  if(run.chat_enabled){renderChat(run);return;}
  $('#content').innerHTML=`<button class="back-button" id="back">‹ All tasks</button><div class="page-heading"><div><div class="eyebrow">${run.mode==='demo'?'DEMO WORKSPACE':'CLOUD WORKSPACE'}</div><h1>Task activity</h1></div><div class="toolbar">${terminal.has(run.status) && !run.active?'<button id="retry" class="small">Run again</button>':'<button id="cancel" class="small danger">Stop task</button>'}</div></div>
  <div class="task-layout"><section class="task-main"><div class="task-intro"><span class="badge">${run.mode==='demo'?'Demo':'Hermes Agent'}</span><p class="prompt">${esc(run.prompt)}</p></div><div class="task-tabs"><span>Activity</span></div><div class="timeline" id="timeline">${run.events.map(eventHTML).join('')}</div><div id="approvals"></div><div id="artifact-area"></div></section><aside class="details"><div class="card"><h3>Run details</h3><div class="detail-row"><span>Status</span><span id="run-status">${statusLabel(run.status)}</span></div><div class="detail-row"><span>Execution</span><span>${run.mode==='demo'?'Simulated':'Modal sandbox'}</span></div><div class="detail-row"><span>Agent</span><span>${run.mode==='demo'?'Not started':'Hermes'}</span></div><div class="detail-row"><span>Repository</span><span>${run.repo_url?esc(run.repo_url.replace('https://github.com/','')):'None'}</span></div><div class="detail-row"><span>Connections</span><span>${run.plugins.length?run.plugins.map(x=>providerNames[x]).join(', '):'None'}</span></div>${run.sandbox_id?`<div class="detail-row"><span>Sandbox</span><span>${esc(run.sandbox_id)}</span></div>`:''}</div><div class="note"><strong>${run.mode==='demo'?'A preview of the workflow':'An isolated workspace'}</strong>${run.mode==='demo'?'This run uses simulated events. No model, cloud machine, repository, or connected app is accessed.':'Moyai Devin works inside a dedicated Modal sandbox. Writes to connected apps require your approval.'}</div></aside></div>`;
  $('#back').onclick=()=>navigate('tasks').catch(showError);
  if($('#cancel'))$('#cancel').onclick=async()=>{try{await api(`/api/runs/${id}/cancel`,{method:'POST'});await openRun(id);}catch(e){toast(e.message);}};
  if($('#retry'))$('#retry').onclick=async()=>{await navigate('tasks');$('#prompt').value=run.prompt;$('#repo').value=run.repo_url;$('#mode').value=run.mode;$('#mode').dispatchEvent(new Event('change'));document.querySelectorAll('[name="plugin"]').forEach(input=>input.checked=run.plugins.includes(input.value));};
  renderApprovals(run.approvals || []);
  if(run.has_artifact)$('#artifact-area').innerHTML=`<div class="artifact-download"><a href="/api/runs/${id}/artifact">Download result and changes (.zip)</a></div>`;
  if(!terminal.has(run.status) || run.active){
    const cursor=run.events.at(-1)?.id||0; const source=new EventSource(`/api/runs/${id}/events?after=${cursor}`);state.source=source;
    source.onmessage=(e)=>{if(state.selected!==id)return;const event=JSON.parse(e.data);$('#timeline').insertAdjacentHTML('beforeend',eventHTML(event));if(event.kind==='approval') refreshApproval(id).catch(showError);};
    source.addEventListener('run-status',e=>{if(state.selected===id)$('#run-status').innerHTML=statusLabel(JSON.parse(e.data).status);});
    source.addEventListener('settled',()=>{source.close();if(state.selected===id)openRun(id).catch(showError);});
    source.onerror=()=>{if(source.readyState===EventSource.CLOSED)toast('Activity stream disconnected. Reopen this task to reconnect.');};
  }
}
function toggleDetails(open){
  state.detailsOpen=open;
  $('.chat-layout')?.classList.toggle('show-details',open);
  if($('#session-details'))$('#session-details').hidden=!open;
  $('#toggle-details')?.setAttribute('aria-expanded',String(open));
}
function renderChat(run){
  const id=run.id;
  $('#header-actions').innerHTML=`<span id="run-status"></span><button id="toggle-details" class="quiet details-toggle" aria-expanded="false" aria-controls="session-details"><span aria-hidden="true">☷</span> Activity</button>`;
  $('#content').innerHTML=`<div class="chat-layout"><section class="chat-panel"><div class="conversation" id="conversation" role="log" aria-label="Conversation" aria-live="polite"></div><button id="jump-latest" class="jump-latest" hidden>↓ Latest message</button><div class="chat-bottom"><div id="approvals"></div><div class="chat-working" id="chat-working" role="status"></div><form id="message-form" class="composer reply-composer"><label class="sr-only" for="followup">Message Moyai Devin</label><textarea id="followup" maxlength="16000" required rows="1" placeholder="Ask a follow-up or give the next step…"></textarea><div class="composer-toolbar">${run.mode==='demo'?'<span class="composer-model">Demo session</span>':modelPicker('chat-model',state.modelDrafts[id]||run.model||state.config.model)}<span id="connection-state" class="connection-notice" hidden>Reconnecting…</span><button id="stop-response" class="stop-button" type="button" aria-label="Stop response" title="Stop response"><span aria-hidden="true">■</span></button><button type="submit" class="send-button" aria-label="Send message" title="Send message"><span aria-hidden="true">↑</span></button></div></form><div class="composer-caption"><span id="queue-note">Your conversation and files stay here.</span><span>Shift + Enter for a new line</span></div></div></section>
  <aside class="session-side" id="session-details" aria-label="Session details" hidden><div class="details-heading"><h2>Session activity</h2><button id="close-details" class="icon-button" aria-label="Close session details">×</button></div><div class="session-facts">${run.owner?`<div class="detail-row"><span>Started by</span><span>${esc(run.owner.email||run.owner.name)}</span></div>`:''}<div class="detail-row"><span>Connected apps</span><span>${run.plugins.length?run.plugins.map(x=>providerNames[x]).join(', '):'None selected'}</span></div><div class="detail-row"><span>Workspace</span><span id="saved-workspace"></span></div><div id="artifact-area"></div></div><div id="slack-context"></div><section class="activity-panel"><h3>Progress</h3><div class="timeline" id="timeline">${run.events.filter(e=>!['chat','result'].includes(e.kind)).map(eventHTML).join('')}</div></section></aside></div>`;
  $('#toggle-details').onclick=()=>toggleDetails(!state.detailsOpen);$('#close-details').onclick=()=>toggleDetails(false);toggleDetails(state.detailsOpen);
  $('#followup').value=state.drafts[id]||'';
  if(run.parent_run_id){$('#followup').disabled=true;$('#followup').placeholder='Send instructions in the coordinator session.';$('#message-form [type="submit"]').hidden=true;if($('#chat-model'))$('#chat-model').disabled=true;}
  if($('#chat-model'))$('#chat-model').onchange=()=>{state.modelDrafts[id]=$('#chat-model').value;};
  $('#followup').oninput=()=>{state.drafts[id]=$('#followup').value;};
  bindComposer($('#followup'),$('#message-form'));
  const bottom=()=>{const box=$('#conversation');box.scrollTop=box.scrollHeight;};
  $('#jump-latest').onclick=bottom;
  $('#conversation').onscroll=()=>{const box=$('#conversation');$('#jump-latest').hidden=box.scrollHeight-box.scrollTop-box.clientHeight<120;};
  $('#stop-response').onclick=async()=>{try{await api(`/api/runs/${id}/cancel`,{method:'POST'});await refreshChat(id);}catch(e){toast(e.message);}};
  $('#message-form').onsubmit=async e=>{
    e.preventDefault();const content=$('#followup').value.trim();if(!content||state.sending.has(id))return;
    const button=$('#message-form [type="submit"]');button.disabled=true;state.sending.add(id);
    const model=$('#chat-model')?.value||run.model||state.config.model;
    let pending=state.pendingMessages[id];if(!pending||pending.content!==content||pending.model!==model)pending=state.pendingMessages[id]={content,model,client_id:crypto.randomUUID()};
    try{await api(`/api/runs/${id}/messages`,{method:'POST',body:JSON.stringify(pending)});delete state.pendingMessages[id];if(state.modelDrafts[id]===model)delete state.modelDrafts[id];if(state.drafts[id]?.trim()===content)state.drafts[id]='';if(state.selected===id&&$('#followup')?.value.trim()===content){$('#followup').value='';autoSize($('#followup'));}await refreshChat(id);if(state.selected===id)bottom();}
    catch(error){toast(error.message);}finally{state.sending.delete(id);if(state.selected===id&&$('#message-form'))$('#message-form [type="submit"]').disabled=false;}
  };
  updateChat(run,true);
  connectChatStream(run);
}
function connectChatStream(run){
  const id=run.id;let cursor=run.events.at(-1)?.id||0;
  const connect=()=>{
    if(state.selected!==id)return;
    const source=new EventSource(`/api/runs/${id}/events?after=${cursor}`);state.source=source;
    const current=()=>state.selected===id&&state.source===source;
    source.onopen=()=>{if(current()){$('#connection-state').hidden=true;refreshChat(id).catch(showError);}};
    source.onmessage=e=>{
      if(!current())return;
      const event=JSON.parse(e.data);if(event.id<=cursor)return;cursor=event.id;
      if(!['chat','result'].includes(event.kind))$('#timeline').insertAdjacentHTML('beforeend',eventHTML(event));
      if(['chat','approval','artifact','context','agents'].includes(event.kind))refreshChat(id).catch(showError);
    };
    source.addEventListener('run-status',e=>{if(current()){$('#connection-state').hidden=true;updateChatStatus(JSON.parse(e.data));}});
    source.onerror=()=>{
      if(!current())return;
      $('#connection-state').hidden=false;
      // HTTP errors during a deploy can permanently close native EventSource.
      // Reopen from the last received event without replacing the composer.
      source.close();clearTimeout(state.streamRetry);
      state.streamRetry=setTimeout(()=>{state.streamRetry=null;if(current())connect();},3000);
    };
  };
  connect();
}
function updateChatStatus(run){
  if(run.model&&$('#chat-model')&&!state.modelDrafts[state.selected])$('#chat-model').value=run.model;
  $('#run-status').innerHTML=statusLabel(run.status);
  const busy=!terminal.has(run.status)||run.active;
  $('#stop-response').hidden=!busy;$('#stop-response').disabled=run.status==='stopping';
  $('#queue-note').textContent=run.slack_mirroring==='active'?'Your messages and replies are shared with the connected Slack conversation.':run.slack_mirroring==='paused'?'Slack sharing is paused for this session.':busy?'Follow-ups queue after this response.':'Your conversation and files stay here.';
  if($('#followup').disabled)$('#queue-note').textContent='The coordinator manages this worker’s instructions and results.';
  $('#chat-working').classList.toggle('busy',busy);
  $('#chat-working').textContent=run.checkpoint_error&&terminal.has(run.status)?'The latest workspace files were not saved; see the warning above.':({idle:'',queued:'Waiting to start…',provisioning:'Opening your workspace…',running:'Moyai is working…',saving:'Saving your work…',awaiting_approval:'Waiting for administrator approval',waiting_children:'Parallel agents are working; this coordinator has released its sandbox.',stopping:'Stopping…',failed:'This response failed. Details are shown above; your conversation is saved.',cancelled:'Response stopped. You can continue from here.',interrupted:'Response interrupted. Send a message to continue.'})[run.status]||'';
  if(busy&&run.active_model)$('#chat-working').textContent+=' · '+modelName(run.active_model);
  const item=state.runs.find(r=>r.id===state.selected);if(item&&item.status!==run.status){item.status=run.status;renderSidebar();}
}
function updateChat(run,initial=false){
  const box=$('#conversation');const atBottom=initial||box.scrollHeight-box.scrollTop-box.clientHeight<100;
  const signature=JSON.stringify(run.messages);
  if(box.dataset.messages!==signature){
    box.dataset.messages=signature;
    box.innerHTML=`<div class="conversation-inner">${run.messages.map((m,index)=>{const failure=m.role==='assistant'&&['failed','cancelled','interrupted'].includes(m.status)?m.status:m.role==='assistant'&&['failed','cancelled','interrupted'].includes(run.messages[index-1]?.status)?run.messages[index-1].status:null;return `<article class="chat-message ${m.role==='user'?'user':'assistant'} ${failure?'response-error':''}"><div class="message-label">${m.role==='user'?(m.user_id&&m.user_id===state.userId?'You':esc(m.user_name||'Earlier message')):'<img src="/static/favicon.svg?v=agent-2" alt="">Moyai Devin'}<small>${m.role==='user'?(m.status!=='completed'?esc(m.status):''):m.status==='save_failed'?'Answer saved · workspace save failed':failure?'Response '+esc(failure):run.mode==='demo'?'Demo':m.model?esc(modelName(m.model)):''}</small></div><div class="message-content ${m.role==='user'?'plain-text':'markdown'}">${m.role==='user'?esc(m.content):renderMarkdown(m.content)}</div>${m.role==='assistant'?`<button class="copy-message quiet" data-message="${m.id}" aria-label="Copy response" title="Copy response">⧉</button>`:''}</article>`;}).join('')}</div>`;
    box.querySelectorAll('.copy-message').forEach(b=>b.onclick=()=>copyText(run.messages.find(m=>String(m.id)===b.dataset.message).content,b));
    box.querySelectorAll('.copy-code').forEach(b=>b.onclick=()=>copyText(b.closest('.code-block').querySelector('code').textContent,b));
    if(atBottom)box.scrollTop=box.scrollHeight;
  }
  $('#jump-latest').hidden=box.scrollHeight-box.scrollTop-box.clientHeight<120;
  updateChatStatus(run);renderApprovals(run.approvals||[]);renderSlackContext(run.slack_source);renderAgentTeam(run.agents);
  $('#saved-workspace').textContent=run.mode==='demo'?'Simulated':run.checkpoint_error?(run.snapshot_id?'Latest save failed; earlier checkpoint retained':'Latest save failed; no saved checkpoint'):run.snapshot_id?'Saved for follow-ups':'Preparing';
  $('#artifact-area').innerHTML=run.has_artifact?`<a class="session-download" href="/api/runs/${run.id}/artifact">↓ Download latest files</a>`:'';
  $('#message-form [type="submit"]').disabled=state.sending.has(run.id);
}
async function copyText(text,button){try{await navigator.clipboard.writeText(text);const label=button.textContent;button.textContent='Copied';setTimeout(()=>button.textContent=label,1800);}catch{toast('Could not copy. You can select and copy the text.');}}
function renderAgentTeam(team){
  let target=$('#agent-team');
  if(!target){target=document.createElement('section');target.id='agent-team';target.className='agent-team';$('#conversation').before(target);}
  const signature=JSON.stringify(team);if(target.dataset.team===signature)return;target.dataset.team=signature;
  target.hidden=!team?.parent_id&&!team?.groups?.length;if(target.hidden)return;
  target.dataset.active=String(team.groups.some(g=>!g.settled));
  target.innerHTML=team.parent_id?`<a href="#run=${esc(team.parent_id)}">← Coordinator session</a>`:
    `<details open><summary>Parallel agents · ${team.groups.reduce((n,g)=>n+g.completed,0)} / ${team.groups.reduce((n,g)=>n+g.total,0)} completed${team.spend!==null?' · '+dollars(team.spend)+' including coordinator':''}</summary>${team.missing_costs?'<p class="subtext">Some request costs have not arrived.</p>':''}<div class="agent-grid">${team.groups.flatMap(g=>g.children).map(c=>`<article class="agent-worker"><a href="#run=${esc(c.id)}">${esc(c.agent_label)}</a>${statusLabel(c.status)}${c.cost?`<small>${dollars(c.cost.spend)}${c.cost.missing_costs?' · cost pending or missing':''}</small>`:''}${c.has_artifact?`<a class="agent-files" href="/api/runs/${esc(c.id)}/artifact">Download files</a>`:''}${c.checkpoint_error?'<small>Latest workspace save failed</small>':''}</article>`).join('')}</div></details>`;
}
function renderSlackContext(source){
  const target=$('#slack-context');if(!target)return;const signature=JSON.stringify(source);if(target.dataset.source===signature)return;target.dataset.source=signature;
  if(!source){target.innerHTML='';return;}
  const ready=source.context_status==='ready', messages=source.messages||[];
  const status=ready?`${messages.length} messages from the ${source.kind}`:({pending:'Waiting to read the conversation…',fetching:'Reading the conversation…',unavailable:'Conversation could not be read',legacy:'This session started before automatic Slack context'})[source.context_status]||'Context unavailable';
  target.innerHTML=`<section class="card source-context"><h3>Slack context</h3><p>${esc(status)}</p>${source.permalink?`<a href="${esc(source.permalink)}" target="_blank" rel="noopener noreferrer">Open source conversation ↗</a>`:''}${source.warning?`<p class="source-warning">${esc(source.warning)}</p>`:''}${ready?`<details><summary>View included messages</summary><div class="source-messages">${messages.map(m=>`<article><small>${esc(m.user)} · ${new Date(Number(m.ts)*1000).toLocaleString()}</small><p>${esc(m.text)}${m.text_truncated?'…':''}</p></article>`).join('')}</div></details>`:''}</section>`;
}
async function refreshChat(id){const version=state.chatRefresh=(state.chatRefresh||0)+1;const run=await api(`/api/runs/${id}`);if(version===state.chatRefresh&&state.selected===id&&$('#conversation'))updateChat(run);}
function eventHTML(event){const stamp=new Date(event.created_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});const detail=event.data?.command||event.data?.detail;return `<div class="event event-${esc(event.kind)}"><span class="event-marker">${event.kind==='result'?'✓':event.kind==='tool'?'⌘':'·'}</span><div class="event-heading"><strong>${esc(event.message)}</strong><time>${stamp}</time></div>${detail?`<details class="event-detail"><summary>Details</summary><pre>${esc(typeof detail==='string'?detail:JSON.stringify(detail,null,2))}</pre></details>`:''}</div>`;}
function renderApprovals(approvals){ if(!$('#approvals'))return;$('#approvals').innerHTML=approvals.filter(a=>a.status==='pending').map(a=>`<div class="approval"><h3>Approval needed: ${esc(a.tool)}</h3><pre>${esc(JSON.stringify(a.arguments,null,2))}</pre>${state.role==='admin'?`<div class="approval-actions"><button data-approval="${a.id}" data-decision="approve" class="primary small">Approve once</button><button data-approval="${a.id}" data-decision="deny" class="small">Deny</button></div>`:'<p>An organization administrator must approve this action.</p>'}</div>`).join('');document.querySelectorAll('[data-approval]').forEach(b=>b.onclick=async()=>{try{await api(`/api/approvals/${b.dataset.approval}`,{method:'POST',body:JSON.stringify({decision:b.dataset.decision})});await refreshApproval(state.selected);}catch(e){toast(e.message);}});}
async function refreshApproval(id){if(state.selected!==id)return;const run=await api(`/api/runs/${id}`);if(state.selected===id){renderApprovals(run.approvals);$('#run-status').innerHTML=statusLabel(run.status);}}
async function renderConnections(){
  const version=state.pageVersion;const [connections,organization]=await Promise.all([api('/api/connections'),api('/api/organization')]);if(version!==state.pageVersion)return;state.connections=connections;state.organization=organization;renderSidebar();
  const admin=state.role==='admin', org=state.organization, slack=org.slack_sessions;
  $('#content').innerHTML=`<div class="page-heading"><div><div class="eyebrow">${esc(org.name)} / ORGANIZATION SETTINGS</div><h1>Organization connections</h1><p class="subtext">Connect once. Share approved tools across your team's sessions.</p></div><span class="badge">${admin?'Administrator':'Member'}</span></div>
    <div class="org-summary"><div><strong>Shared with your organization</strong><p>Each session chooses which apps it uses. Credentials stay on the server.</p></div><span>${state.connections.filter(c=>c.connected).length} of ${state.connections.length} connected</span></div>
    <div class="connections-grid org-connections">${state.connections.map(c=>`<article class="connection-card"><div class="connection-top"><div class="app-icon ${c.id}">${providerNames[c.id][0]}</div><span class="connection-state ${c.connected&&c.enabled?'healthy':''}">${c.connected?(c.enabled?'Connected':'Paused'):'Not connected'}</span></div><h2>${providerNames[c.id]}</h2><p>${({linear:'Issues, requirements, and progress comments.',slack:'Conversation search and thread context.',notion:'Documentation, page context, and new notes.',github:'Read code and open normal PRs. PR approval and merging are blocked.'})[c.id]}</p><dl class="connection-facts"><div><dt>Workspace</dt><dd>${esc(c.label||'—')}</dd></div><div><dt>Connection</dt><dd>${esc(c.identity)}</dd></div><div><dt>Access</dt><dd>${c.read_only?'Read only':'Writes need admin approval'}</dd></div></dl><small>${c.check_status==='healthy'?'Verified '+relative(c.checked_at):c.check_status==='needs_attention'?'Connection needs attention':'Ready for a connection check'}</small><div class="connection-action"><button data-manage="${c.id}">${admin?'Manage':'View access'}</button></div></article>`).join('')}</div>
    <div class="org-lower"><section class="card org-slack"><div class="section-header"><h2>Start sessions from Slack</h2><span class="connection-state ${slack.enabled?'healthy':''}">${slack.enabled?'Enabled':'Setup needed'}</span></div><p>Mention <strong>@Moyai Devin</strong> with a task in a channel the bot has joined. It reacts with 👀 when it picks up your message, then posts its answer in the thread. ${slack.thread_reply_ready?'Reply there to continue the same conversation; no repeat mention is needed.':'Mention the bot again in the same thread to continue. An administrator can reconnect Slack to enable replies without a mention.'}</p><p class="subtext">${slack.enabled?esc(slack.audience)+' can start sessions using enabled organization apps.':'The Slack workspace bot must be installed before mentions can start sessions.'} Everyone in that Slack thread can see the replies. Use stop, sleep, or wake to control the conversation. External write approvals stay in the web app.</p><p>${slack.direct_message_ready?'You can also message Moyai directly. Follow-up DMs keep the same conversation and files.':'Direct messages need the bot’s DM access and message event subscription.'} DM sessions are also visible to signed-in BerriAI teammates in Moyai.</p>${slack.last_session?`<small>Latest request ${relative(slack.last_session.created_at)} · ${slack.last_session.reply_status==='sent'?'Reply sent':'Reply '+esc(slack.last_session.reply_status)}</small>`:''}</section>
    <section class="card org-access"><h2>Organization access</h2><p>Administrators manage connections and approve external writes. Members can start sessions and use enabled apps.</p><p class="subtext">${org.google_signin?'Sign in with your BerriAI Google account. Administrators are assigned by email; other teammates join as members.':org.member_access_configured?'Separate administrator and member sign-ins are enabled.':'Currently using the shared administrator sign-in. Separate member access is not configured.'}</p>${admin?`<form id="org-name-form"><label for="org-name">Organization name</label><div class="inline-field"><input id="org-name" maxlength="80" required value="${esc(org.name)}"><button type="submit">Save</button></div></form>`:''}</section></div>
    <div class="note org-note"><strong>Shared connection, same provider identity</strong>A shared connection uses the account that authorized it. Sharing it here does not turn a personal account into a service account or expand its provider permissions. Conversational replies are posted automatically to the originating Slack conversation. Writes to connected systems, including messages to other Slack conversations, still require administrator approval.</div>
    ${org.activity.length?`<section class="org-history"><h2>Connection activity</h2>${org.activity.map(a=>`<div><span>${esc(providerNames[a.provider]||'Organization')} · ${esc(a.action)}</span><small>${esc(a.actor)} · ${relative(a.created_at)}</small></div>`).join('')}</section>`:''}`;
  document.querySelectorAll('[data-manage]').forEach(b=>b.onclick=()=>manageConnection(b.dataset.manage));
  if($('#org-name-form'))$('#org-name-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/organization',{method:'PATCH',body:JSON.stringify({name:$('#org-name').value})});await renderConnections();toast('Organization updated.');}catch(err){toast(err.message);}};
}
function manageConnection(provider){
  const c=state.connections.find(c=>c.id===provider), name=providerNames[provider], admin=state.role==='admin';
  $('#connection-title').textContent=name+' · Organization access';
  $('#connection-body').innerHTML=`<p>${c.connected?'Connected to '+esc(c.label)+' using '+esc(c.identity.toLowerCase())+'.':'No shared connection is installed.'}</p><ul class="tool-list">${c.tools.map(t=>`<li>${esc(t.description)}</li>`).join('')}</ul>${admin?`<label class="policy-check"><input id="connection-enabled" type="checkbox" ${c.enabled?'checked':''}>Available to organization sessions</label><div class="field"><label for="connection-access">Allowed actions</label><select id="connection-access"><option value="approval" ${!c.read_only?'selected':''}>Read and request approval for writes</option><option value="read" ${c.read_only?'selected':''}>Read only</option></select></div><p class="subtext">Pausing access or switching to read only also cancels pending write approvals. An action already sent cannot be recalled.</p><button class="primary full" type="submit">Save access</button><div class="connection-controls">${c.connected?'<button id="connection-check" type="button">Check connection</button>':''}<button id="connection-reconnect" type="button">${c.connected?'Reconnect':'Connect'} ${name}</button>${c.connected?'<button id="connection-disconnect" class="danger" type="button">Disconnect</button>':''}</div>`:`<div class="note">${c.enabled?'Available to sessions.':'Paused for all sessions.'} ${c.read_only?'Read only.':'Writes require administrator approval.'} Ask an organization administrator to change this connection.</div>`}<div id="connection-error" role="alert"></div>`;
  $('#connection-form').onsubmit=async e=>{e.preventDefault();if(!admin)return;try{await api('/api/connections/'+provider+'/policy',{method:'PATCH',body:JSON.stringify({enabled:$('#connection-enabled').checked,read_only:$('#connection-access').value==='read'})});$('#connection-dialog').close();await renderConnections();toast(name+' access updated.');}catch(error){$('#connection-error').textContent=error.message;}};
  if($('#connection-reconnect'))$('#connection-reconnect').onclick=()=>{ $('#connection-dialog').close();connectionDialog(provider); };
  if($('#connection-check'))$('#connection-check').onclick=async()=>{const b=$('#connection-check');b.disabled=true;try{await api('/api/connections/'+provider+'/check',{method:'POST'});toast(name+' connection verified.');await renderConnections();}catch(error){$('#connection-error').textContent=error.message;}finally{b.disabled=false;}};
  if($('#connection-disconnect'))$('#connection-disconnect').onclick=async()=>{const b=$('#connection-disconnect');if(b.dataset.confirm!=='yes'){b.dataset.confirm='yes';b.textContent='Disconnect for everyone';return;}try{await api('/api/connections/'+provider,{method:'DELETE'});$('#connection-dialog').close();await renderConnections();toast(name+' disconnected.');}catch(error){$('#connection-error').textContent=error.message;}};
  $('#connection-dialog').showModal();
}
function connectionDialog(provider){
  const connection=state.connections.find(c=>c.id===provider),name=providerNames[provider];
  const hints={linear:'Authorize the Linear account or integration your organization should use. Its existing team permissions still apply.',slack:'Connect the Slack account whose conversations your organization can search. The workspace bot handles session mentions separately.',notion:'Connect Notion and choose the pages your organization can use in sessions.'};
  $('#connection-title').textContent='Connect '+name+' for your organization';
  if(provider==='github'){
    $('#connection-body').innerHTML='<p>Install the organization GitHub App for the configured repository. Teammates share this connection; no personal GitHub sign-in is needed.</p><p>Moyai can read code and open normal pull requests after administrator approval. It cannot approve or merge PRs, enable auto-merge, update existing branches, or change workflows and access controls.</p><button class="primary full" type="submit">Continue with GitHub</button><div id="connection-error" role="alert"></div>';
    $('#connection-form').onsubmit=async e=>{e.preventDefault();const button=$('#connection-form button[type="submit"]');button.disabled=true;try{const result=await api('/api/connections/github/oauth',{method:'POST'});location.assign(result.url);}catch(error){$('#connection-error').textContent=error.message;button.disabled=false;}};
    $('#connection-dialog').showModal();return;
  }

  $('#connection-body').innerHTML='<p>'+hints[provider]+'</p>'+(connection.oauth_configured?'<button class="primary full" id="oauth-connect" type="button">Continue with '+name+'</button><div class="divider">or use a token</div>':'<div class="note">One-click sign-in becomes available after an administrator configures this app’s OAuth client.</div>')+'<div class="field"><label for="connection-token">Access token</label><input id="connection-token" class="token-input" type="password" required minlength="8" maxlength="4096" autocomplete="off"><small>Validated with '+name+' and stored encrypted on the server.</small></div><div id="connection-error" role="alert"></div><button class="primary full" type="submit">Connect '+name+'</button>';
  $('#connection-form').onsubmit=async(e)=>{
    e.preventDefault();const button=$('#connection-form button[type="submit"]');button.disabled=true;
    try{await api('/api/connections/'+provider,{method:'POST',body:JSON.stringify({token:$('#connection-token').value})});$('#connection-token').value='';$('#connection-dialog').close();await renderConnections();toast(name+' connected.');}
    catch(error){$('#connection-error').textContent=error.message;button.disabled=false;}
  };
  if($('#oauth-connect'))$('#oauth-connect').onclick=async()=>{try{const result=await api('/api/connections/'+provider+'/oauth',{method:'POST'});window.location.assign(result.url);}catch(e){$('#connection-error').textContent=e.message;}};
  $('#connection-dialog').showModal();
}
async function renderRuntime(){const version=state.pageVersion;const config=await api('/api/config');if(version!==state.pageVersion)return;state.config=config;const fields=['MODAL_TOKEN_ID','MODAL_TOKEN_SECRET','LITELLM_API_BASE','LITELLM_API_KEY','AGENT_MODEL',...state.config.missing.filter(x=>x.startsWith('PUBLIC_URL'))];$('#content').innerHTML=`<div class="page-heading"><div><div class="eyebrow">EXECUTION</div><h1>Runtime</h1><p class="subtext">One isolated workspace for every cloud task.</p></div><span class="badge">${state.config.cloud_ready?'Cloud ready':'Setup needed'}</span></div><div class="setup-grid"><div class="card"><h2>Modal + Hermes</h2><p class="subtext">Modal provides the machine. Hermes plans the work, uses tools, and returns the result.</p><ul class="config-list">${fields.map(key=>`<li><code>${key}</code><span class="${state.config.missing.includes(key)?'missing':'configured'}">${state.config.missing.includes(key)?'Not configured':'Configured'}</span></li>`).join('')}</ul></div><div class="card"><h2>Workspace settings</h2><div class="detail-row"><span>Session recovery</span><span>${esc(state.config.execution_engine||"Local worker")}${state.config.execution_connected===false?" · reconnecting":""}</span></div>${state.config.checkpoint_interval_seconds?`<div class="detail-row"><span>Save progress</span><span>Every ${Math.round(state.config.checkpoint_interval_seconds/60)} minutes between tool rounds</span></div>`:""}<div class="detail-row"><span>Maximum active + idle sandboxes</span><span>${state.config.max_concurrent_runs}</span></div><div class="detail-row"><span>Parallel agents</span><span>${state.config.parallel_agents_enabled?'Up to '+state.config.max_parallel_agents+' workers per group':'Requires Temporal'}</span></div><div class="detail-row"><span>Idle sessions</span><span>${state.config.sandbox_idle_seconds?Math.round(state.config.sandbox_idle_seconds/60)+' minutes of sandbox reuse':'No running sandbox'}</span></div><div class="detail-row"><span>Response time limit</span><span>${state.config.run_timeout_seconds?Math.round(state.config.run_timeout_seconds/60)+' minutes':'No app limit'}</span></div><div class="detail-row"><span>Model</span><span>${esc(state.config.model||'Choose in .env')}</span></div><p class="subtext" style="margin-top:25px">Your administrator manages the cloud connection. Each response runs in an isolated workspace and saves its files for the next message. Long tasks automatically continue on a fresh machine before Modal’s 24-hour limit.${state.config.sandbox_idle_seconds?' Idle sandboxes can be released sooner when another session needs capacity. Finished workers and coordinators waiting for workers release immediately.':''}</p></div></div>`;}
function showError(error){toast(error.message);}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>navigate(b.dataset.view).catch(showError));
$('#new-task').onclick=()=>navigate('tasks').then(()=>$('#prompt')?.focus()).catch(showError);
$('#session-list').onclick=e=>{const button=e.target.closest('[data-run]');if(button)openRun(button.dataset.run).catch(showError);};
$('#session-search').oninput=renderSidebar;
$('#open-sidebar').onclick=()=>setSidebar(true);$('#close-sidebar').onclick=()=>setSidebar(false);$('#sidebar-scrim').onclick=()=>setSidebar(false);
window.addEventListener('keydown',e=>{if(e.key==='Escape'){setSidebar(false);if(state.selected&&$('.chat-layout'))toggleDetails(false);}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'&&state.csrf){e.preventDefault();$('#new-task').click();}});
$('.dialog-close').onclick=()=>$('#connection-dialog').close();
window.addEventListener('hashchange',()=>{
  if(!state.csrf)return;
  const linkedRun=location.hash.match(/^#run=([a-f0-9]{32})$/)?.[1];
  (linkedRun?openRun(linkedRun):navigate(['#connections','#runtime','#spend'].includes(location.hash)?location.hash.slice(1):'tasks')).catch(showError);
});
async function boot(){
  try{
    const session=await api('/api/session');state.authenticated=session.authenticated;state.csrf=session.csrf;state.role=session.role||'member';state.userId=session.user_id;
    $('#spend-nav').hidden=!session.authenticated||state.role!=='admin';
    $('.rail-foot small').textContent=session.local?'Private · local preview':'Shared internal workspace';
    if(!session.authenticated){
      const passwordForm='<form id="login-form"><div class="field"><label for="password">Workspace password</label><input id="password" type="password" autocomplete="current-password" required></div><button class="primary full">Sign in</button></form>';
      $('#content').innerHTML=`<div class="login"><div class="card"><div class="login-mark" aria-hidden="true">M</div><h1>Welcome to Moyai</h1><p class="subtext">${session.google_enabled?'Your team’s agent, one conversation away.':'Sign in to Moyai Devin.'}</p>${session.google_enabled?`<button id="google-signin" class="google-signin full" type="button"><span class="google-letter" aria-hidden="true">G</span> Continue with Google</button><p class="login-domain">Use your ${session.google_domains.map(d=>'@'+esc(d)).join(' or ')} work account.</p>`:''}${session.password_enabled?(session.google_enabled?'<details class="password-fallback"><summary>Use workspace password</summary>'+passwordForm+'</details>':passwordForm):''}<p id="signin-error" class="signin-error" role="alert"></p></div></div>`;
      if($('#login-form'))$('#login-form').onsubmit=async(e)=>{e.preventDefault();try{await api('/api/login',{method:'POST',body:JSON.stringify({password:$('#password').value})});await boot();}catch(error){toast(error.message);}};
      if($('#google-signin'))$('#google-signin').onclick=async()=>{const button=$('#google-signin');button.disabled=true;try{const result=await api('/api/auth/google/start',{method:'POST',body:JSON.stringify({return_to:'/'+location.hash})});location.assign(result.url);}catch(error){button.disabled=false;$('#signin-error').textContent=error.message;}};
      const signInError=new URLSearchParams(location.search).get('signin');
      if(signInError){$('#signin-error').textContent=signInError==='cancelled'?'Sign-in was cancelled. You can try again.':'We couldn’t sign you in. Use your BerriAI Google Workspace account and try again.';history.replaceState(null,'','/'+location.hash);}
      document.querySelectorAll('.rail button').forEach(b=>b.disabled=true);
      return;
    }
    document.querySelectorAll('.rail button').forEach(b=>b.disabled=false);
    [state.config,state.organization]=await Promise.all([api('/api/config'),api('/api/organization')]);await refreshRuns();
    if(!session.local){$('.rail-foot small').textContent=session.identity?session.identity.email:state.role==='admin'?'Organization admin':'Organization member';$('.rail-foot small').title=state.role==='admin'?'Organization admin':'Organization member';$('#logout')?.remove();$('.rail-foot').insertAdjacentHTML('beforeend','<button id="logout" class="quiet" aria-label="Sign out">⏻</button>');$('#logout').onclick=async()=>{await api('/api/logout',{method:'POST'});location.reload();};}
    const linkedRun=location.hash.match(/^#run=([a-f0-9]{32})$/)?.[1]; if(linkedRun)await openRun(linkedRun);else await navigate(['#connections','#runtime','#spend'].includes(location.hash)?location.hash.slice(1):'tasks');
    if(new URLSearchParams(location.search).get('connection')){toast(location.search.includes('success')?'App connected.':'Connection cancelled.');history.replaceState(null,'','/#connections');}
    registerWebMCP();
  }catch(e){$('#content').innerHTML='<div class="error-banner">'+esc(e.message)+'</div>';}
}
function registerWebMCP(){
  if(!document.modelContext?.registerTool)return;
  const abort=new AbortController();window.addEventListener('pagehide',()=>abort.abort(),{once:true});
  const tools=[
    {name:'list_workspace_tasks',description:'List the latest tasks in this workspace.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true},execute:async()=>api('/api/runs')},
    {name:'create_demo_task',description:'Create a simulated task to preview the workspace; no model, cloud machine, or connected app is used.',inputSchema:{type:'object',properties:{prompt:{type:'string',minLength:3,maxLength:16000}},required:['prompt'],additionalProperties:false},annotations:{readOnlyHint:false},execute:async(input)=>{if(typeof input.prompt!=='string'||input.prompt.trim().length<3)throw Error('Enter a task of at least three characters.');const run=await api('/api/runs',{method:'POST',body:JSON.stringify({prompt:input.prompt,mode:'demo'})});await refreshRuns();await openRun(run.id);return {id:run.id,status:run.status,mode:run.mode};}}
  ];
  tools.forEach(tool=>{try{Promise.resolve(document.modelContext.registerTool(tool,{signal:abort.signal})).catch(()=>{});}catch{}});
}
matchMedia('(max-width:850px)').addEventListener('change',()=>setSidebar(false));
setInterval(()=>{if(state.authenticated&&!document.hidden)refreshRuns().catch(()=>{});},15000);
boot();

setInterval(()=>{if(state.authenticated&&!document.hidden&&state.selected&&$('#agent-team')?.dataset.active==='true')refreshChat(state.selected).catch(()=>{});},10000);
