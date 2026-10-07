const $ = (s) => document.querySelector(s);
const esc = (s = '') => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {view:'tasks', runs:[], folders:[], closedFolders:new Set(), runsRefresh:0, config:{missing:[]}, connections:[], organization:{}, role:'member', selected:null, source:null, csrf:'', drafts:{}, modelDrafts:{}, pendingMessages:{}, pageVersion:0, newDraft:{}, detailsOpen:false, sending:new Set(), expandedParents:new Set(), activeParentId:'',queueDrafts:{}};
const terminal = new Set(['completed','failed','cancelled','interrupted','idle']);
let workspacePanel;
const savedFiles = MoyaiFiles.create({api,markdown:renderMarkdown,escape:esc,size:fileSize,onOpen:file=>workspacePanel?(file?workspacePanel.openFile(file):workspacePanel.open('files')):false});
const computer = MoyaiComputer.create({api,escape:esc,onCapture:file=>workspacePanel?.openFile(file)});
const providerNames = {linear:'Linear', slack:'Slack', notion:'Notion', github:'GitHub'};
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers:{'Content-Type':'application/json', 'X-CSRF-Token':state.csrf, ...options.headers}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const upload = options.method === 'PUT' && path.startsWith('/api/attachments/');
    const fallback = upload ? (
      response.status === 403 ? 'The hosting firewall blocked this upload (HTTP 403). Refresh the page and retry the file.' :
      response.status === 413 ? 'This file is too large. Attach a file under 10 MB.' :
      response.status === 429 ? 'Other files are uploading. Retry this file in a moment.' :
      response.status >= 500 ? `The upload service is temporarily unavailable (HTTP ${response.status}). Retry this file.` :
      `Upload failed (HTTP ${response.status}). Retry this file.`
    ) : `The request could not be completed (HTTP ${response.status}).`;
    throw new Error(typeof body.detail === 'string' ? body.detail : fallback);
  }
  return body;
}
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(state.toast); state.toast = setTimeout(() => $('#toast').hidden = true, 5500); }
function statusLabel(status) { return `<span class="status ${esc(status)}">${esc(status==='idle'?'Ready':status.replaceAll('_',' '))}</span>`; }
function relative(date) { const min = Math.max(0, Math.floor((Date.now() - new Date(date)) / 60000)), hours=Math.floor(min/60), days=Math.floor(min/1440), months=Math.floor(days/30); return min < 1 ? 'Just now' : min < 60 ? `${min}m ago` : hours < 24 ? `${hours} hour${hours===1?'':'s'} ago` : days < 30 ? `${days} day${days===1?'':'s'} ago` : months < 2 ? 'last month' : months < 12 ? `${months} months ago` : new Date(date).toLocaleDateString(); }
function stopStream(){ if(typeof workspacePanel!=='undefined'){workspacePanel?.dispose();workspacePanel=null;} computer.close(); clearTimeout(state.streamRetry); state.streamRetry=null; state.source?.close(); state.source = null; state.chatRun = null; savedFiles.reset(); }
function sessionTitle(run={}){
  const text=value=>typeof value==='string'?value.replace(/\s+/g,' ').trim():'';
  for(const value of [run.agent_label,run.display_title]){const title=text(value);if(title)return title;}
  // This is only a deterministic placeholder until the title agent finishes.
  // Keep the request's meaning; do not guess corrections, outcomes or PRs.
  const lines=(typeof run.prompt==='string'?run.prompt:'').split(/\r?\n/).map(line=>line
    .replace(/^\s*(?:(?:[-*+•]|\d+[.)]|#{1,6}|>)\s+)+(?:\[[ xX]\]\s*)?/,'')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g,'$1')
    .replace(/(?:https?:\/\/|www\.)[^\s<>]+/gi,'')
    .replace(/`([^`]+)`|\*\*([^*]+)\*\*/g,(_,code,bold)=>code||bold).replace(/\s+/g,' ').trim());
  let title=lines.find(line=>/[\p{L}\p{N}]/u.test(line))||'';
  title=title.replace(/^(?:(?:please|hey|hi)[,!:]?\s+)?(?:(?:can|could|would) you\s+|(?:we|i)\s+(?:wanna|want to|need to|would like to)\s+|please\s+)/i,'').trim();
  if(!title)return 'Untitled session';
  title=title[0].toUpperCase()+title.slice(1);
  if(title.length>96){const short=title.slice(0,93);title=(short.lastIndexOf(' ')>60?short.slice(0,short.lastIndexOf(' ')):short)+'…';}
  return title;
}
function sessionMatches(run,search){
  const term=search.trim().toLowerCase();
  return [sessionTitle(run),run.agent_label,run.display_title,run.prompt].some(value=>typeof value==='string'&&value.toLowerCase().includes(term));
}
function sessionStatus(run){
  return ({idle:'Ready',completed:'Completed',running:'Working now',queued:'Queued',provisioning:'Starting',reconnecting:'Reconnecting',saving:'Saving',awaiting_approval:'Needs approval',waiting_credential:'Needs access',waiting_children:'Agents working',stopping:'Stopping',failed:'Failed',cancelled:'Stopped',interrupted:'Interrupted'})[run.status]||'Status unknown';
}
function sessionRepository(run){
  try{const url=new URL(run.repo_url);return url.protocol==='https:'&&url.hostname==='github.com'?url.pathname.split('/').filter(Boolean).slice(0,2).join('/').replace(/\.git$/,''):'';}catch{return '';}
}
function syncSessionTitle(run){
  if(run?.id!==state.selected)return;
  const heading=$('#page-title');if(heading){heading.textContent=sessionTitle(run);heading.title=sessionTitle(run);}
}
function syncRunSummary(run){
  if(state.selected===run.id&&state.chatRun?.id===run.id&&!document.hidden)markSessionRead(run);
  const item=state.runs.flatMap(parent=>[parent,...(parent.children||[])]).find(item=>item.id===run.id);
  if(item)for(const key of ['agent_label','display_title','prompt','status','updated_at']){if(Object.hasOwn(run,key))item[key]=run[key];}
  syncSessionTitle(item?{...run,...item}:run);
  renderSidebar();
}
// Read markers are local to this browser and scoped to the signed-in user.
// A newer completion timestamp makes the circle reappear for follow-up work.
function sessionReadKey(run){return `moyai-session-read:${state.userId||'local'}:${run.id}`;}
function sessionCompletion(run){return ['idle','completed'].includes(run.status)?String(run.updated_at||run.created_at||run.status):'';}
function sessionReadMarker(run){
  const key=sessionReadKey(run);state.sessionRead??=new Map();
  if(!state.sessionRead.has(key)){
    let marker='';try{marker=localStorage.getItem(key)||'';}catch{}
    state.sessionRead.set(key,marker);
  }
  return state.sessionRead.get(key);
}
function markSessionRead(run){
  const marker=sessionCompletion(run);if(!marker||sessionReadMarker(run)===marker)return;
  const key=sessionReadKey(run);state.sessionRead.set(key,marker);
  try{localStorage.setItem(key,marker);}catch{}
  state.sessionReadFade??=new Map();state.sessionReadFade.set(key,Date.now()+350);
  state.sidebarSignature=null;
}
function sessionIndicator(run){
  const label=sessionStatus(run);
  if(['running','queued','provisioning','reconnecting','saving','waiting_children','stopping'].includes(run.status))return `<span class="session-indicator" title="${esc(label)}" aria-hidden="true"><span class="session-spinner"></span></span>`;
  const marker=sessionCompletion(run),unread=marker&&sessionReadMarker(run)!==marker;
  const remaining=(state.sessionReadFade?.get(sessionReadKey(run))||0)-Date.now();
  if(unread||marker&&remaining>0)return `<span class="session-indicator" title="${unread?'Unread completion':'Read'}" aria-hidden="true"><span class="session-completion${unread?'':' is-read'}"${unread?'':` style="animation-delay:-${350-remaining}ms"`}></span></span>`;
  if(!marker&&['failed','interrupted','waiting_credential','awaiting_approval'].includes(run.status))return `<span class="session-indicator session-attention ${esc(run.status)}" title="${esc(label)}" aria-hidden="true">!</span>`;
  return '<span class="session-indicator" aria-hidden="true"></span>';
}
function modelName(model=state.config.model){return (state.config.models||[]).find(m=>m.id===model)?.name||model||'Moyai';}
function harnessName(harness='hermes'){return (state.config.harnesses||[]).find(h=>h.id===harness)?.name||harness;}
function harnessLogo(harness){const url=MoyaiProviderLogos.harness(harness);return `<img class="provider-logo harness-logo" alt="" width="16" height="16" ${url?`src="${esc(url)}"`:'hidden'}>`;}
function pickerChevron(){return `<span class="picker-chevron" aria-hidden="true">${globalThis.MoyaiIcon?.('chevron',14)||''}</span>`;}
function harnessPicker(selected=state.config.harness||'claude-agent-sdk'){return `<label class="model-picker harness-picker" title="Engine for the new session">${harnessLogo(selected)}<span class="sr-only">Engine for new session</span><select id="new-harness" aria-label="Engine for new session">${(state.config.harnesses||[{id:'claude-agent-sdk',name:'Claude Agent SDK'}]).map(h=>`<option value="${esc(h.id)}" ${h.id===selected?'selected':''}>${esc(h.name)}</option>`).join('')}</select>${pickerChevron()}</label>`;}
function harnessModels(harness){return state.config.models||[];}
function providerLogo(model){const url=MoyaiProviderLogos.src(model);return `<img class="provider-logo" alt="" width="16" height="16" ${url?`src="${esc(url)}"`:'hidden'}>`;}
function modelPicker(id,selected,disabled=false,harness=state.config.harness||'claude-agent-sdk'){return `<label class="model-picker" title="Model">${providerLogo(selected)}<span class="sr-only">Model for next message</span><select id="${id}" aria-label="Model for next message" ${disabled?'disabled':''}>${harnessModels(harness).map(m=>`<option value="${esc(m.id)}" ${m.id===selected?'selected':''}>${esc(m.name)}</option>`).join('')}</select>${pickerChevron()}</label>`;}

function openSessionSearch(){$('.session-search').hidden=false;$('#search-sessions').setAttribute('aria-expanded','true');$('#session-search').focus();}
function closeSessionSearch(){if($('#session-search').value)return;$('.session-search').hidden=true;$('#search-sessions').setAttribute('aria-expanded','false');}
function setSidebar(open){document.body.classList.toggle('sidebar-open',open);$('#sidebar-scrim').hidden=!open;$('#open-sidebar').setAttribute('aria-expanded',String(open));$('#sidebar').inert=matchMedia('(max-width:850px)').matches&&!open;}
function sidebarGroups(runs,search){
  return runs.map(parent=>{
    const children=parent.children||[];
    const parentMatch=sessionMatches(parent,search);
    const visible=parentMatch?children:children.filter(child=>sessionMatches(child,search));
    return {...parent,children:visible,totalChildren:children.length,visible:parentMatch||visible.length>0};
  }).filter(parent=>parent.visible);
}
function sidebarRow(run,child=false){
  const selected=state.selected===run.id,title=sessionTitle(run),label=sessionStatus(run),repo=sessionRepository(run);
  const date=run.updated_at||run.created_at,time=date&&Number.isFinite(Date.parse(date))?relative(date):'';
  const unread=sessionCompletion(run)&&sessionReadMarker(run)!==sessionCompletion(run);
  const context=[child?'Agent':run.side_chat_of?'Side chat':'',run.mode==='demo'?'Demo':'',repo].filter(Boolean).join(' · ');
  return `<button class="session-link ${child?'child-session ':''}${selected?'selected':''}" data-run="${esc(run.id)}" ${child?'':`draggable="true" data-drag-session="${esc(run.id)}"`} ${selected?'aria-current="page"':''} aria-label="${esc([title,label,unread?'Unread completion':'',context,time].filter(Boolean).join(' · '))}" title="${esc(title)}">${child?'<span class="child-mark" aria-hidden="true">·</span>':''}<span class="session-link-body"><span class="session-title-row"><span class="session-link-title">${esc(title)}</span>${sessionIndicator(run)}</span><span class="session-link-meta">${time?`<span class="session-updated" data-session-time="${esc(run.id)}" title="${esc('Updated '+new Date(date).toLocaleString())}">${esc(time)}</span>`:''}</span>${context?`<span class="session-link-context" title="${esc(context)}">${esc(context)}</span>`:''}</span></button>`;
}
function sidebarSections(runs,folders,search){
  const ids=new Set(folders.map(folder=>folder.id));
  const sections=folders.map(folder=>{
    const members=runs.filter(run=>run.folder_id===folder.id);
    return {...folder,groups:sidebarGroups(members,folder.name.toLowerCase().includes(search)?'':search)};
  }).filter(folder=>!search||folder.groups.length||folder.name.toLowerCase().includes(search));
  const recent=sidebarGroups(runs.filter(run=>!ids.has(run.folder_id)),search);
  return {folders:sections,recent};
}
function sidebarRenderSessions(groups,search){return groups.map(parent=>{
    const hasChildren=parent.totalChildren>0,expanded=!!search||state.expandedParents.has(parent.id);
    return `<div class="session-group"><div class="parent-session">${hasChildren?`<button class="agent-disclosure" data-toggle-agents="${esc(parent.id)}" aria-label="${expanded?'Collapse':'Expand'} agents for ${esc(sessionTitle(parent))}" aria-expanded="${expanded}" aria-controls="children-${esc(parent.id)}"><span aria-hidden="true">${expanded?'⌄':'›'}</span></button>`:'<span class="agent-disclosure-space"></span>'}${sidebarRow(parent)}<button class="session-move" data-move-session="${esc(parent.id)}" title="Move to folder" aria-label="Move ${esc(sessionTitle(parent))} to folder">⋯</button></div>${hasChildren?`<div class="child-sessions" id="children-${esc(parent.id)}" role="group" aria-label="Agents for ${esc(sessionTitle(parent))}" ${expanded?'':'hidden'}>${parent.children.map(child=>sidebarRow(child,true)).join('')}</div>`:''}</div>`;
  }).join('');}
function renderSidebar(){
  const summaries=state.runs.flatMap(parent=>[parent,...(parent.children||[])]),selected=summaries.find(run=>run.id===state.selected);
  if(selected){
    if(state.chatRun?.id===selected.id)for(const key of ['agent_label','display_title','prompt']){if(Object.hasOwn(selected,key))state.chatRun[key]=selected[key];}
    syncSessionTitle(selected);
  }
  if(typeof workspacePanel!=='undefined')workspacePanel?.syncTitles?.(summaries);
  // Polling must not replace the source element during a native drag.
  if(state.draggedSessionId)return;
  const search=($('#session-search').value||'').trim().toLowerCase();
  const sections=sidebarSections(state.runs,state.folders,search);
  $('#task-count').textContent=state.runs.length;
  $('#workspace-name').textContent=state.organization.name||'Workspace';
  const signature=JSON.stringify([state.selected,search,[...state.expandedParents],[...state.closedFolders],sections]);
  if(state.sidebarSignature===signature)return;
  state.sidebarSignature=signature;
  const list=$('#session-list'),scroll=list.scrollTop;
  const focusAttrs=['data-toggle-agents','data-toggle-folder','data-edit-folder','data-move-session','data-run'];
  const focused=focusAttrs.map(attr=>[attr,document.activeElement?.getAttribute(attr)]).find(([,value])=>value);
  list.innerHTML=sections.folders.map(folder=>{
    const expanded=!!search||!state.closedFolders.has(folder.id);
    return `<section class="session-folder" data-drop-folder="${esc(folder.id)}"><div class="folder-heading"><button class="folder-toggle" data-toggle-folder="${esc(folder.id)}" aria-expanded="${expanded}" aria-controls="folder-${esc(folder.id)}"><span class="folder-chevron" aria-hidden="true">${expanded?'⌄':'›'}</span>${sessionFolderIcon}<span class="folder-name">${esc(folder.name)}</span><span class="folder-count">${folder.groups.length}</span></button><button class="folder-menu" data-edit-folder="${esc(folder.id)}" title="Rename or remove folder" aria-label="Rename or remove ${esc(folder.name)}">⋯</button></div><div class="folder-sessions" id="folder-${esc(folder.id)}" ${expanded?'':'hidden'}>${sidebarRenderSessions(folder.groups,search)||'<p class="folder-empty">Drop a session here or use its ⋯ menu.</p>'}</div></section>`;
  }).join('')+(state.folders.length?`<section class="unfiled-sessions ${sections.recent.length?'':'unfiled-empty'}" data-drop-folder=""><div class="unfiled-heading">Recent · not in a folder</div>${sidebarRenderSessions(sections.recent,search)||'<p class="folder-empty">Drop here to remove from folder.</p>'}</section>`:sidebarRenderSessions(sections.recent,search));
  if(!sections.folders.length&&!sections.recent.length)list.innerHTML=`<p class="sidebar-empty">${search?'No matching folders, sessions or agents.':'Your conversations will appear here.'}</p>`;
  list.scrollTop=scroll;
  if(focused)list.querySelector(`[${focused[0]}="${CSS.escape(focused[1])}"]`)?.focus();
}
function setView(view,title){
  state.skillComposer?.destroy();state.skillComposer=null;
  state.attachments?.destroy();state.attachments=null;
  document.body.classList.toggle('chat-view',view==='chat');
  document.body.classList.toggle('home-view',view==='tasks');
  document.body.classList.toggle('settings-view',settingsViews.has(view));
  $('#settings-navigation').hidden=!settingsViews.has(view);
  if(settingsViews.has(view))updateSettingsNavigation();
  document.title = title ? `${title} · Moyai` : 'Moyai';
  document.querySelectorAll('.nav-button').forEach(b=>{
    const active=b.dataset.view===view||(b.dataset.view==='settings'&&settingsViews.has(view));
    b.classList.toggle('active',active);
    if(active)b.setAttribute('aria-current',b.dataset.view===view?'page':'true');else b.removeAttribute('aria-current');
  });
  const breadcrumb=settingsViews.has(view)&&view!=='settings';
  $('#page-title').title=title;
  $('#page-title').classList.toggle('settings-breadcrumb',breadcrumb);
  if(breadcrumb)$('#page-title').innerHTML=`<a href="#settings">Settings</a><span aria-hidden="true">/</span><span aria-current="page">${esc(title)}</span>`;
  else $('#page-title').textContent=title;
  $('#header-actions').innerHTML='';
  setSidebar(false);renderSidebar();
}
function autoSize(input){input.style.height='auto';input.style.height=Math.min(input.scrollHeight,200)+'px';}
function bindComposer(input,form){
  input.addEventListener('input',()=>autoSize(input));
  const skills=bindInlineSkillPicker(input,form);state.skillComposer=skills;
  state.attachments=bindAttachments(input,form,input.id==='prompt'?'new':state.selected);
  input.addEventListener('keydown',e=>{if(skills.keydown(e))return;if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();const now=(e.ctrlKey||e.metaKey)&&input.id==='followup';if(now&&!input.value.trim()&&!state.attachments.hasFiles()){state.messageQueue?.sendFirst();return;}if(!form.querySelector('[type="submit"]').disabled)form.requestSubmit(now?form.querySelector('[data-send-now]'):undefined);}});
  autoSize(input);
}
async function navigate(view) {
  stopStream();state.pageVersion++;state.view=view;state.selected=null;
  const version=state.pageVersion;
  setView(view,{settings:'Settings',automations:'Automations',tasks:'New session',connections:'Connections',runtime:'Runtime',spend:'Spend',adoption:'Adoption',users:'Users',environments:'Environments',secrets:'Secrets',skills:'Skills',memory:'Memory'}[view]);
  history.replaceState(null,'',view==='tasks'?'#tasks':'#'+view);
  if(settingsViews.has(view)) {
    $('#content').innerHTML='<p class="settings-loading" role="status">Loading…</p>';
    $('#content').scrollTop=0;
  }
  try {
  if(view==='settings')await renderSettings();else if(view==='automations')await renderAutomations();else if(view==='tasks')await renderHome();else if(view==='connections')await renderConnections();else if(view==='adoption')await renderAdoption();else if(view==='spend')await renderSpend();else if(view==='users')await renderUsers();else if(view==='environments')await renderEnvironments();else if(view==='secrets')await renderSecrets();else if(view==='skills')await renderSkills();else if(view==='memory')await renderMemory();else await renderRuntime();
  } catch(error) {
    if(version!==state.pageVersion)return;
    if(!settingsViews.has(view))throw error;
    settingsLoadError($('#page-title').title,error,()=>navigate(view));
  }
  if(version===state.pageVersion&&settingsViews.has(view)) {
    updateSettingsNavigation();
    const heading=$('#content h1');
    if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});}
  }
}
async function refreshRuns(){const refresh=++state.runsRefresh,focus=state.selected||location.hash.match(/^#run=([a-f0-9]{32})$/)?.[1]||'';const params=new URLSearchParams({scope:state.role==='admin'&&state.sessionScope==='all'?'all':'mine'});if(focus)params.set('focus',focus);const [runs,folders]=await Promise.all([api('/api/runs?'+params),api('/api/session-folders')]);if(refresh!==state.runsRefresh)return;state.runs=runs;state.folders=folders.folders;renderSidebar();}
function restoreSessionScope(){
  const canViewAll=state.authenticated&&state.role==='admin';
  state.sessionScope='mine';
  state.sessionScopeKey='moyai-session-scope:'+state.userId;
  try{if(canViewAll&&localStorage.getItem(state.sessionScopeKey)==='all')state.sessionScope='all';else if(!canViewAll)localStorage.setItem(state.sessionScopeKey,'mine');}catch{}
  $('#session-scope').innerHTML='<option value="mine">My sessions</option>'+(canViewAll?'<option value="all">All sessions</option>':'');
  $('#session-scope').value=state.sessionScope;
  $('#session-scope').disabled=!canViewAll;
  $('#session-scope').title=state.userId?.startsWith('google:')?'Sessions you created or messaged in, including linked Slack activity. Opening a link alone does not count.':'Sessions created or messaged in by this shared login. Sign in with Google for a personal view.';
}
async function changeSessionScope(){
  state.sessionScope=state.role==='admin'&&$('#session-scope').value==='all'?'all':'mine';
  $('#session-scope').value=state.sessionScope;
  try{localStorage.setItem(state.sessionScopeKey,state.sessionScope);}catch{}
  state.runs=[];renderSidebar();
  try{await refreshRuns();}catch(error){showError(error);}
}
async function renderHome(){
  const version=state.pageVersion;
  const [,connections,environments]=await Promise.all([refreshRuns(),api('/api/connections'),api('/api/environments')]);
  if(version!==state.pageVersion)return;state.connections=connections;
  const apps=connections.filter(c=>c.connected&&c.enabled),draft=state.newDraft;
  $('#content').innerHTML=`<section class="new-conversation"><div class="welcome-mark"><img src="/static/favicon.svg?v=moyai-train-1" alt=""><span>Moyai</span></div><h1>What are we working on?</h1><p class="welcome-note">A teammate for your code, questions, and next steps.</p>
    <form id="task-form" class="composer"><textarea id="prompt" name="prompt" aria-label="Message Moyai" placeholder="Ask Moyai to build, investigate, or pick up a thread…" required minlength="3" maxlength="16000" rows="3"></textarea><div class="composer-toolbar"><details class="task-options"><summary aria-label="Session options">${globalThis.MoyaiIcon?.('sliders',16)||''}<span>Context</span></summary><div class="task-settings"><div class="field"><label for="repo">GitHub repository</label><input id="repo" type="url" placeholder="https://github.com/owner/repo" value="${esc(draft.repo||'')}"></div><div class="field"><label for="project-environment">Project environment</label><select id="project-environment">${environmentOptions(environments,draft.environment_id||'auto')}</select></div><div class="field"><label for="mode">Execution</label><select id="mode"><option value="modal" ${state.config.cloud_ready?'':'disabled'}>Cloud session</option><option value="demo" ${state.config.cloud_ready?'':'selected'}>Demo · simulated</option></select></div><div id="plugin-options"><span>Organization connections</span>${apps.map(c=>`<label class="plugin-toggle"><input type="checkbox" name="plugin" value="${c.id}" ${!draft.plugins||draft.plugins.includes(c.id)?'checked':''}>${providerNames[c.id]}</label>`).join('')||'<small>Connect apps in organization settings.</small>'}</div></div></details><button type="button" class="quiet skill-picker-button" data-skill-picker="prompt" aria-label="Choose a skill" title="Skills · or type /">${globalThis.MoyaiIcon?.('slash',16)||'/'}</button><span class="composer-pickers">${harnessPicker(draft.harness)}${modelPicker('new-model',draft.model||state.config.model,false,draft.harness)}</span><button class="send-button" type="submit" aria-label="Start session" title="Start session">${globalThis.MoyaiIcon?.('up',18)||'↑'}</button></div></form>
    <div class="composer-caption"><span id="mode-note">${state.config.cloud_ready?'Your own cloud workspace':'Demo responses · audio uses transcription'}</span><span>Type / for skills · Enter to send</span></div>
    <div class="suggestions"><button type="button" data-prompt="Help me investigate a bug. "><span aria-hidden="true">⌘</span> Investigate a bug</button><button type="button" data-prompt="Read the repository and explain how it works. Make no changes. "><span aria-hidden="true">⌑</span> Explore a codebase</button><button type="button" data-prompt="Find the team context for "><span aria-hidden="true">⌕</span> Find team context</button></div><p class="connected-note">${apps.length?`<span class="connected-dot"></span>${apps.map(c=>providerNames[c.id]).join(', ')} connected`:'Add your team’s apps in Connections'}</p></section>`;
  $('#prompt').value=draft.prompt||'';
  if(draft.mode&&($('#mode option[value="'+draft.mode+'"]').disabled===false))$('#mode').value=draft.mode;
  const saveDraft=()=>{state.newDraft={prompt:$('#prompt').value,repo:$('#repo').value,environment_id:$('#project-environment').value,mode:$('#mode').value,model:$('#new-model').value,harness:$('#new-harness').value,plugins:[...document.querySelectorAll('[name="plugin"]:checked')].map(x=>x.value)};};
  $('#task-form').oninput=saveDraft;$('#task-form').onchange=saveDraft;$('#task-form').onsubmit=submitTask;
  $('#new-harness').onchange=()=>{const picker=$('#new-model'),selected=picker.value;picker.innerHTML=harnessModels($('#new-harness').value).map(m=>`<option value="${esc(m.id)}" ${m.id===selected?'selected':''}>${esc(m.name)}</option>`).join('');MoyaiProviderLogos.sync(picker.parentElement.querySelector('.provider-logo'),picker.value);MoyaiProviderLogos.sync($('#new-harness').parentElement.querySelector('.harness-logo'),$('#new-harness').value,MoyaiProviderLogos.harness);saveDraft();};
  $('#mode').addEventListener('change',()=>{$('#new-model').disabled=$('#mode').value==='demo';$('#mode-note').textContent=$('#mode').value==='demo'?'Demo responses · audio uses transcription':'Your own cloud workspace';});
  $('#mode').dispatchEvent(new Event('change'));
  bindComposer($('#prompt'),$('#task-form'));
  document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('#prompt').value=b.dataset.prompt;saveDraft();autoSize($('#prompt'));$('#prompt').focus();});
}
async function submitTask(e){
  e.preventDefault();if(state.sending.has('new'))return;
  const files=state.attachments;let attachment_ids;
  try{attachment_ids=files.ids();}catch(error){toast(error.message);return;}
  const form=$('#task-form'),input=$('#prompt'),submittedPrompt=input.value;
  const button=form.querySelector('button[type="submit"]');button.disabled=true;state.sending.add('new');files.lock(true);
  const body={prompt:$('#prompt').value.trim()||(attachment_ids.length?'Please respond to the attached files and audio transcripts.':''),repo_url:$('#repo').value,environment_id:$('#project-environment').value,mode:$('#mode').value,model:$('#new-model').value,harness:$('#new-harness').value,plugins:[...document.querySelectorAll('[name="plugin"]:checked')].map(x=>x.value),attachment_ids};
  const signature=JSON.stringify(body);if(state.pendingNew?.signature!==signature)state.pendingNew={signature,client_id:crypto.randomUUID()};
  try{
    const run=await api('/api/runs',{method:'POST',body:JSON.stringify({...body,client_id:state.pendingNew.client_id})});
    // Clear the submitted DOM value too: removing a focused textarea fires a
    // change event, which would otherwise save the old prompt back into newDraft.
    // Keep text edited while the request was in flight, and keep drafts on failure.
    if(input.value===submittedPrompt){input.value='';autoSize(input);}
    if(state.newDraft.prompt===submittedPrompt)state.newDraft={};
    files.clear(attachment_ids);state.pendingNew=null;
    await refreshRuns();await openRun(run.id);
  }
  catch(error){toast(error.message);}finally{state.sending.delete('new');files.lock(false);if(button.isConnected)button.disabled=false;}
}
async function openRun(id){
  stopStream();const version=++state.pageVersion;state.selected=id;const run=await api(`/api/runs/${id}`);if(version!==state.pageVersion)return;state.activeParentId=run.parent_run_id||'';if(run.parent_run_id||run.agents?.groups?.length)state.expandedParents.add(run.parent_run_id||id);if(!state.runs.some(r=>r.id===(run.parent_run_id||id)))await refreshRuns();if(version!==state.pageVersion)return;if(!document.hidden)markSessionRead(run);state.view='tasks';setView(run.chat_enabled?'chat':'legacy',sessionTitle(run));history.replaceState(null,'','#run='+id);
  if(run.chat_enabled){renderChat(run);return;}
  $('#content').innerHTML=`<button class="back-button" id="back">‹ All tasks</button><div class="page-heading"><div><div class="eyebrow">${run.mode==='demo'?'DEMO WORKSPACE':'CLOUD WORKSPACE'}</div><h1>Task activity</h1></div><div class="toolbar">${terminal.has(run.status) && !run.active?'<button id="retry" class="small">Run again</button>':'<button id="cancel" class="small danger">Stop task</button>'}</div></div>
  <div class="task-layout"><section class="task-main"><div class="task-intro"><span class="badge">${run.mode==='demo'?'Demo':esc(harnessName(run.harness))}</span><p class="prompt">${esc(run.prompt)}</p></div><div class="task-tabs"><span>Activity</span></div><div class="timeline" id="timeline">${run.events.map(eventHTML).join('')}</div><div id="approvals"></div><div id="artifact-area"></div></section><aside class="details"><div class="card"><h3>Run details</h3><div class="detail-row"><span>Status</span><span id="run-status">${statusLabel(run.status)}</span></div><div class="detail-row"><span>Execution</span><span>${run.mode==='demo'?'Simulated':'Modal sandbox'}</span></div><div class="detail-row"><span>Agent</span><span>${run.mode==='demo'?'Not started':esc(harnessName(run.harness))}</span></div><div class="detail-row"><span>Repository</span><span>${run.repo_url?esc(run.repo_url.replace('https://github.com/','')):'None'}</span></div><div class="detail-row"><span>Connections</span><span>${run.plugins.length?run.plugins.map(x=>providerNames[x]).join(', '):'None'}</span></div>${run.sandbox_id?`<div class="detail-row"><span>Sandbox</span><span>${esc(run.sandbox_id)}</span></div>`:''}</div><div class="note"><strong>${run.mode==='demo'?'A preview of the workflow':'An isolated workspace'}</strong>${run.mode==='demo'?'This run uses simulated events. No model, cloud machine, repository, or connected app is accessed.':'Moyai works inside a dedicated Modal sandbox. Writes to connected apps require your approval.'}</div></aside></div>`;
  $('#back').onclick=()=>navigate('tasks').catch(showError);
  if($('#cancel'))$('#cancel').onclick=async()=>{try{await api(`/api/runs/${id}/cancel`,{method:'POST'});await openRun(id);}catch(e){toast(e.message);}};
  if($('#retry'))$('#retry').onclick=async()=>{await navigate('tasks');$('#prompt').value=run.prompt;$('#repo').value=run.repo_url;$('#mode').value=run.mode;$('#mode').dispatchEvent(new Event('change'));document.querySelectorAll('[name="plugin"]').forEach(input=>input.checked=run.plugins.includes(input.value));};
  renderApprovals(run.approvals || []);
  savedFiles.sync(run);
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
  if(open)workspacePanel?.open('activity');else workspacePanel?.hide();
}
function initializeChatControls(){
  $('#chat-working').insertAdjacentHTML('beforebegin','<section id="goal-status" class="goal-status" aria-label="Session goal" hidden></section>');
  $('#session-details .activity-panel').innerHTML='<h3>Current activity</h3><p id="activity-summary"></p><details class="activity-history"><summary>Tool history <span id="activity-count"></span></summary><div id="activity-history"></div></details>';
}
function renderChat(run){
  const id=run.id;
  state.activityHistoryLimit=5;
  const icon=(name,size=17)=>globalThis.MoyaiIcon?.(name,size)||'';
  $('#header-actions').innerHTML=`${(run.parent_run_id||run.side_chat_of)?`<button class="quiet parent-session-link" data-open-parent="${esc(run.parent_run_id||run.side_chat_of)}" title="Open parent session">← Parent session</button>`:''}<span id="run-status"></span>${run.mode==='modal'?`<button id="computer-button" class="quiet details-toggle header-icon" aria-label="Computer" title="Computer" aria-controls="workspace-panel">${icon('monitor')}</button>`:''}<button id="files-button" class="quiet details-toggle" aria-controls="workspace-panel" hidden>Files</button><button id="toggle-details" class="quiet details-toggle header-icon" aria-label="Activity" title="Activity" aria-expanded="false" aria-controls="session-details">${icon('list')}</button><button id="workspace-panel-toggle" class="quiet details-toggle header-icon" aria-label="Show workspace panel" title="Workspace panel" aria-controls="workspace-panel" aria-expanded="false">${icon('panel')}</button>`;
  $('#content').innerHTML=`<div class="chat-layout"><section class="chat-panel"><div class="conversation" id="conversation" role="log" aria-label="Conversation" aria-live="polite"></div><button id="jump-latest" class="jump-latest" hidden>↓ Latest message</button><div class="chat-bottom"><div id="approvals"></div><div class="chat-working" id="chat-working" role="status"></div><section id="message-queue" class="message-queue" aria-label="Queued messages" hidden></section><form id="message-form" class="composer reply-composer"><label class="sr-only" for="followup">Message Moyai</label><textarea id="followup" maxlength="16000" required rows="1" placeholder="Respond to Moyai or ask something else"></textarea><div class="composer-toolbar"><button type="button" class="quiet skill-picker-button" data-skill-picker="followup" aria-label="Choose a skill" title="Skills · or type /">${icon('slash',16)}</button>${run.mode==='demo'?'<span class="composer-model">Demo session</span>':modelPicker('chat-model',state.modelDrafts[id]||run.model||state.config.model,false,run.harness)}<span id="connection-state" class="connection-notice" hidden>Reconnecting…</span><button id="stop-response" class="stop-button" type="button" aria-label="Stop response" title="Stop response"><span aria-hidden="true">■</span></button><button type="submit" class="send-button" aria-label="Send message" title="Send message">${icon('up',18)}</button><button type="submit" data-send-now hidden>Send now</button></div></form><div class="composer-caption"><span id="queue-note">Your conversation and files stay here.</span><span>Type / for skills · Shift + Enter for a new line</span></div></div></section>
  <aside class="session-side" id="session-details" aria-label="Session details" hidden><div class="details-heading"><h2>Session activity</h2><button id="close-details" class="icon-button" aria-label="Close session details">×</button></div><div class="session-facts">${run.owner?`<div class="detail-row"><span>Started by</span><span>${esc(run.owner.email||run.owner.name)}</span></div>`:''}${run.project_environment?.name?`<div class="detail-row"><span>Project environment</span><span>${esc(run.project_environment.name)} · ${esc(run.project_environment.commit_sha.slice(0,8))}</span></div>`:''}<div class="detail-row"><span>Connected apps</span><span>${run.plugins.length?run.plugins.map(x=>providerNames[x]).join(', '):'None selected'}</span></div><div class="detail-row"><span>Workspace</span><span id="saved-workspace"></span></div><div id="artifact-area"></div></div><div id="slack-context"></div><div id="agent-details"></div><section class="activity-panel"><h3>Progress</h3><div class="timeline" id="timeline">${run.events.filter(e=>!['chat','result'].includes(e.kind)).map(eventHTML).join('')}</div></section></aside></div>`;
  workspacePanel=MoyaiPanel.create({run,layout:$('.chat-layout'),api,computer,markdown:renderMarkdown,escape:esc,size:fileSize,titleFor:sessionTitle,matchesSession:sessionMatches,user:state.userId||'shared:local:admin',models:harnessModels(run.harness),toast,onCreated:()=>refreshRuns().catch(showError)});
  $('#workspace-panel-toggle').onclick=()=>workspacePanel.toggle();
  $('#toggle-details').onclick=()=>workspacePanel.open('activity');$('#close-details').onclick=()=>workspacePanel.hide();
  if($('#computer-button'))$('#computer-button').onclick=()=>workspacePanel.open('computer');
  initializeChatControls();
  $('#followup').value=state.drafts[id]||'';
  if(run.parent_run_id)$('#followup').placeholder='Message this agent directly…';
  document.querySelector('[data-open-parent]')?.addEventListener('click',()=>openRun(run.parent_run_id||run.side_chat_of).catch(showError));
  if($('#chat-model'))$('#chat-model').onchange=()=>{state.modelDrafts[id]=$('#chat-model').value;};
  $('#followup').oninput=()=>{state.drafts[id]=$('#followup').value;};
  bindComposer($('#followup'),$('#message-form'));
  state.messageQueue=MoyaiQueue.create({element:$('#message-queue'),runId:id,user:state.userId,role:state.role,sendImmediately:()=>state.preferences?.send_immediately===true,api,refresh:refreshChat,toast,attachments:messageAttachments,preview:showAttachment,focusComposer:()=>{if(state.selected===id&&(document.activeElement===document.body||$('#message-queue')?.contains(document.activeElement)))$('#followup')?.focus();},drafts:state.queueDrafts[id]??=new Map(),useDraft:content=>{const input=$('#followup');input.value=[input.value,content].filter(Boolean).join('\n\n');input.dispatchEvent(new Event('input'));input.focus();}});
  const bottom=()=>{const box=$('#conversation');box.scrollTop=box.scrollHeight;};
  $('#jump-latest').onclick=bottom;
  $('#conversation').onscroll=()=>{const box=$('#conversation');$('#jump-latest').hidden=box.scrollHeight-box.scrollTop-box.clientHeight<120;};
  $('#stop-response').onclick=async()=>{try{await api(`/api/runs/${id}/cancel`,{method:'POST'});await refreshChat(id);}catch(e){toast(e.message);}};
  $('#message-form').onsubmit=async e=>{
    e.preventDefault();if(state.sending.has(id))return;
    const send_now=e.submitter?.hasAttribute('data-send-now')||false;
    const files=state.attachments;let attachment_ids;
    try{attachment_ids=files.ids();}catch(error){toast(error.message);return;}
    const original=$('#followup').value.trim(),content=original||(attachment_ids.length?'Please respond to the attached files and audio transcripts.':'');if(!content)return;
    const button=$('#message-form [type="submit"]');button.disabled=true;state.sending.add(id);files.lock(true);
    const model=$('#chat-model')?.value||run.model||state.config.model;
    let pending=state.pendingMessages[id];if(!pending||pending.content!==content||pending.model!==model||pending.send_now!==send_now||JSON.stringify(pending.attachment_ids)!==JSON.stringify(attachment_ids))pending=state.pendingMessages[id]={content,model,attachment_ids,send_now,client_id:crypto.randomUUID()};
    try{await api(`/api/runs/${id}/messages`,{method:'POST',body:JSON.stringify(pending)});files.clear(attachment_ids);delete state.pendingMessages[id];if(state.modelDrafts[id]===model)delete state.modelDrafts[id];if(state.drafts[id]?.trim()===original)state.drafts[id]='';if(state.selected===id&&$('#followup')?.value.trim()===original){$('#followup').value='';$('#followup').dispatchEvent(new Event('input',{bubbles:true}));autoSize($('#followup'));}await refreshChat(id);if(state.selected===id)bottom();}
    catch(error){toast(error.message);}finally{state.sending.delete(id);files.lock(false);if(state.selected===id&&$('#message-form'))$('#message-form [type="submit"]').disabled=false;}
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
    source.onopen=()=>{if(current()){$('#connection-state').hidden=true;renderLiveWork(null,false);refreshChat(id).catch(showError);}};
    source.onmessage=e=>{
      if(!current())return;
      const event=JSON.parse(e.data);if(event.id<=cursor)return;cursor=event.id;renderLiveWork(event,false);
      if(event.data?.phase==='goal'&&event.data.goal_version===1){state.chatRun.goal=event.data.goal;MoyaiGoal.render($('#goal-status'),state.chatRun,esc);}
      if(['chat','approval','artifact','context','agents','credential'].includes(event.kind)||(event.data?.phase==='steering'&&event.data?.message_id)||
        (event.data?.live_status===true&&MoyaiActivity.isFocus(event)&&String(event.data.input_id)!==String(MoyaiActivity.current(state.chatRun).input)))refreshChat(id).catch(showError);
    };
    source.addEventListener('run-status',e=>{if(current()){$('#connection-state').hidden=true;updateChatStatus(JSON.parse(e.data));}});
    source.onerror=()=>{
      if(!current())return;
      $('#connection-state').hidden=false;renderLiveWork(null,true);
      // HTTP errors during a deploy can permanently close native EventSource.
      // Reopen from the last received event without replacing the composer.
      source.close();clearTimeout(state.streamRetry);
      state.streamRetry=setTimeout(()=>{state.streamRetry=null;if(current())connect();},3000);
    };
  };
  connect();
}
function updateChatStatus(run){
  if(state.chatRun){Object.assign(state.chatRun,run);state.messageQueue?.render(state.chatRun);MoyaiActivity.sync($('#conversation'),state.chatRun,{markdown:renderMarkdown,copy:copyText});savedFiles.decorate($('#conversation'));}
  run=state.chatRun||run;
  if(run.model&&$('#chat-model')&&!state.modelDrafts[state.selected]){$('#chat-model').value=run.model;MoyaiProviderLogos.sync($('#chat-model').parentElement.querySelector('.provider-logo'),run.model);}
  $('#run-status').innerHTML=statusLabel(run.status);
  const busy=!terminal.has(run.status)||run.active;
  $('#stop-response').hidden=!busy;$('#stop-response').disabled=run.status==='stopping';
  const immediate=state.preferences?.send_immediately===true;
  const send=$('#message-form .send-button');if(send){send.title=busy?(immediate?'Send now':'Queue message'):'Send message';send.setAttribute('aria-label',send.title);}if($('#followup'))$('#followup').placeholder=busy?(immediate?'Send a follow-up now…':'Queue a follow-up… (Ctrl/⌘ Enter to send now)'):'Respond to Moyai or ask something else';
  $('#queue-note').textContent=run.slack_mirroring==='active'?'Your messages and replies are shared with the connected Slack conversation.':run.slack_mirroring==='paused'?'Slack sharing is paused for this session.':busy?(immediate?'Enter to send now · Messages guide the active response.':'Enter to queue · Ctrl/⌘ Enter to send now.'):'Your conversation and files stay here.';
  if(state.activeParentId)$('#queue-note').textContent='Chatting with this agent directly. Results already sent to the parent stay saved.';
  renderChatWorking(run);
  syncRunSummary(run);
}
function renderChatWorking(run){
  const current=MoyaiActivity.current(run),node=$('#chat-working');
  node.classList.toggle('busy',current.pulse);
  node.textContent=run.checkpoint_error&&terminal.has(run.status)?'The latest workspace files were not saved; see the warning above.':({idle:'',queued:'Waiting to start…',provisioning:'Opening your workspace…',reconnecting:'Reconnecting to workspace services. Your request will resume automatically…',running:current.headline,saving:'Saving your work…',awaiting_approval:'Waiting for administrator approval',waiting_children:'Parallel agents are working; this coordinator has released its sandbox.',waiting_credential:'Waiting for access. Your work is saved and the sandbox is paused.',stopping:'Stopping…',failed:'This response failed. Details are shown above; your conversation is saved.',cancelled:'Response stopped. You can continue from here.',interrupted:'Response interrupted. Send a message to continue.'})[run.status]||'';
  node.hidden=!node.textContent;
  node.title=node.textContent;
  renderActivitySummary(run);
  MoyaiGoal.render($('#goal-status'),run,esc);
}
function renderActivitySummary(run){
  const target=$('#activity-history');if(!target)return;
  const turns=[...MoyaiActivity.groups(run).values()].filter(turn=>turn.rows.length||turn.live).reverse();
  $('#activity-summary').textContent=MoyaiActivity.current(run).headline||'No active work';
  $('#activity-count').textContent=`· ${turns.reduce((n,turn)=>n+turn.count,0)} actions`;
  const shown=turns.slice(0,state.activityHistoryLimit||5),signature=JSON.stringify(shown);
  if(target.dataset.signature===signature)return;
  target.dataset.signature=signature;
  const existing=new Map([...target.querySelectorAll('[data-history-turn]')].map(node=>[node.dataset.historyTurn,node]));
  target.querySelector('[data-more-history]')?.remove();
  for(const node of [...target.childNodes])if(node.nodeType===3)node.remove();
  for(const turn of shown){
    let slot=existing.get(turn.id);if(!slot){slot=document.createElement('div');slot.dataset.historyTurn=turn.id;}
    // Reuse paired tool rows and omit chat prose / repetitive processing events.
    MoyaiActivity.syncWork(slot,{...turn,rows:turn.rows.filter(row=>row.kind!=='message')});
    target.append(slot);existing.delete(turn.id);
  }
  existing.forEach(node=>node.remove());
  if(!shown.length)target.textContent='No tool activity yet.';
  if(turns.length>shown.length){const more=document.createElement('button');more.className='quiet';more.dataset.moreHistory='';more.textContent='Show earlier turns';more.onclick=()=>{state.activityHistoryLimit+=5;target.dataset.signature='';renderActivitySummary(state.chatRun);};target.append(more);}
}
function updateChat(run,initial=false){
  const previous=state.chatRun?.id===run.id?state.chatRun:null;
  const snapshotCursor=run.events?.at(-1)?.id||0;
  run.events=[...new Map([...(run.events||[]),...(previous?.events||[])].map(event=>[event.id,event])).values()].sort((a,b)=>a.id-b.id);
  const goalEvent=run.events.findLast(event=>event.data?.phase==='goal'&&event.data.goal_version===1);
  if(goalEvent&&goalEvent.id>snapshotCursor)run.goal=goalEvent.data.goal;
  run.activity_disconnected=previous?.activity_disconnected||false;state.chatRun=run;
  const box=$('#conversation');const atBottom=initial||box.scrollHeight-box.scrollTop-box.clientHeight<100;
  state.messageQueue?.render(run);
  const {transcript}=MoyaiQueue.presentation(run);
  const signature=JSON.stringify(transcript);
  if(box.dataset.messages!==signature){
    box.dataset.messages=signature;
    const activitySlots=new Map([...box.querySelectorAll('[data-activity-slot]')].map(slot=>[slot.dataset.activitySlot,slot]));
    box.innerHTML=`<div class="conversation-inner">${transcript.map((m,index)=>{const failure=m.role==='assistant'&&['failed','cancelled','interrupted'].includes(m.status)?m.status:m.role==='assistant'&&['failed','cancelled','interrupted'].includes(transcript[index-1]?.status)?transcript[index-1].status:null;return `<article class="chat-message ${m.role==='user'?'user':'assistant'} ${failure?'response-error':''}"><div class="message-label">${m.role==='user'?esc(m.user_name||'Earlier message'):'<img src="/static/favicon.svg?v=moyai-train-1" alt="">Moyai'}<small>${m.role==='user'?(m.status==='queued'&&m.send_immediately?'Sending…':m.steering_parent_id?'Steering':!['completed','queued'].includes(m.status)?esc(m.status):''):m.status==='save_failed'?'Answer saved · workspace save failed':failure?'Response '+esc(failure):run.mode==='demo'?'Demo':m.model?esc(modelName(m.model)):''}</small></div><div class="message-content ${m.role==='user'?'plain-text':'markdown'}">${m.role==='user'?esc(m.display_content??m.content):renderMarkdown(m.content)}</div>${messageAttachments(m.attachments)}${m.role==='assistant'?`<button class="copy-message quiet" data-message="${m.id}" aria-label="Copy response" title="Copy response">${globalThis.MoyaiIcon?.('copy',16)||'Copy'}</button>`:''}</article>${m.role==='user'?`<div data-activity-slot="${m.id}"></div>`:''}`;}).join('')}<div id="credential-requests"></div></div>`;
    box.querySelectorAll('[data-activity-slot]').forEach(slot=>{const previous=activitySlots.get(slot.dataset.activitySlot);if(previous)slot.replaceWith(previous);});
    MoyaiActivity.sync(box,run,{markdown:renderMarkdown,copy:copyText});
    box.querySelectorAll('[data-attachment]').forEach(button=>button.onclick=()=>showAttachment(run.messages.flatMap(message=>message.attachments||[]).find(file=>file.id===button.dataset.attachment)));
    box.querySelectorAll('.copy-message').forEach(b=>b.onclick=()=>copyText(run.messages.find(m=>String(m.id)===b.dataset.message).content,b));
    box.querySelectorAll('.copy-code').forEach(b=>b.onclick=()=>copyText(b.closest('.code-block').querySelector('code').textContent,b));
    if(atBottom)box.scrollTop=box.scrollHeight;
  }
  $('#jump-latest').hidden=box.scrollHeight-box.scrollTop-box.clientHeight<120;
  updateChatStatus(run);renderCredentialRequests(run.credential_requests||[]);if(atBottom)box.scrollTop=box.scrollHeight;renderApprovals(run.approvals||[]);renderSlackContext(run.slack_source);renderAgentDetails(run.agents);
  $('#saved-workspace').textContent=run.mode==='demo'?'Simulated':run.checkpoint_error?(run.snapshot_id?'Latest save failed; earlier checkpoint retained':'Latest save failed; no saved checkpoint'):run.snapshot_id?'Saved for follow-ups':'Preparing';
  savedFiles.sync(run);
  $('#message-form [type="submit"]').disabled=state.sending.has(run.id);
}
function renderLiveWork(event,disconnected){
  const run=state.chatRun;if(!run||run.id!==state.selected)return;
  run.activity_disconnected=disconnected;
  if(event&&!run.events.some(existing=>existing.id===event.id))run.events.push(event);
  MoyaiActivity.sync($('#conversation'),run,{markdown:renderMarkdown,copy:copyText});
  savedFiles.decorate($('#conversation'));
  renderChatWorking(run);
}
async function copyText(text,button){try{await navigator.clipboard.writeText(text);if(button.dataset.copying)return;const html=button.innerHTML,label=button.getAttribute('aria-label'),title=button.title;button.dataset.copying='true';button.innerHTML=button.matches('.copy-message,.copy-update')?MoyaiIcon('check',16):'Copied';button.setAttribute('aria-label','Copied');button.title='Copied';setTimeout(()=>{button.innerHTML=html;if(label)button.setAttribute('aria-label',label);else button.removeAttribute('aria-label');button.title=title;delete button.dataset.copying;},1800);}catch{toast('Could not copy. You can select and copy the text.');}}
function renderAgentDetails(team){
  const target=$('#agent-details');if(!target)return;
  const signature=JSON.stringify(team);if(target.dataset.team===signature)return;target.dataset.team=signature;
  const children=(team?.groups||[]).flatMap(g=>g.children);
  target.innerHTML=children.length?`<section class="agent-summary"><h3>Parallel agents</h3><p>${children.filter(c=>['idle','completed'].includes(c.status)).length} of ${children.length} ready · Open agents from the sidebar.</p>${team.spend!==null?`<p>${dollars(team.spend)} including parent${team.missing_costs?' · some costs missing':''}</p>`:''}</section>`:'';
  const parent=state.runs.find(r=>r.id===state.selected);
  if(parent&&children.length){
    parent.children=children.map(child=>({...parent.children?.find(c=>c.id===child.id),...child,parent_run_id:parent.id,mode:parent.mode}));
    renderSidebar();
  }
}
function renderSlackContext(source){
  const target=$('#slack-context');if(!target)return;const signature=JSON.stringify(source);if(target.dataset.source===signature)return;target.dataset.source=signature;
  if(!source){target.innerHTML='';return;}
  const ready=source.context_status==='ready', messages=source.messages||[];
  const status=ready?`${messages.length} messages from the ${source.kind}`:({pending:'Waiting to read the conversation…',fetching:'Reading the conversation…',unavailable:'Conversation could not be read',legacy:'This session started before automatic Slack context'})[source.context_status]||'Context unavailable';
  target.innerHTML=`<section class="card source-context"><h3>Slack context</h3><p>${esc(status)}</p>${source.permalink?`<a href="${esc(source.permalink)}" target="_blank" rel="noopener noreferrer">Open source conversation ↗</a>`:''}${source.warning?`<p class="source-warning">${esc(source.warning)}</p>`:''}${ready?`<details><summary>View included messages</summary><div class="source-messages">${messages.map(m=>`<article><small>${esc(m.user)} · ${new Date(Number(m.ts)*1000).toLocaleString()}</small><p>${esc(m.text)}${m.text_truncated?'…':''}</p></article>`).join('')}</div></details>`:''}</section>`;
}
async function refreshChat(id){const version=state.chatRefresh=(state.chatRefresh||0)+1;const run=await api(`/api/runs/${id}`);if(version===state.chatRefresh&&state.selected===id&&$('#conversation'))updateChat(run);}
function eventHTML(event){if(MoyaiActivity.isFocus(event))return '';const stamp=new Date(event.created_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});const detail=event.data?.command||event.data?.detail;return `<div class="event event-${esc(event.kind)}"><span class="event-marker">${event.kind==='result'?'✓':event.kind==='tool'?'⌘':'·'}</span><div class="event-heading"><strong>${esc(event.message)}</strong><time>${stamp}</time></div>${detail?`<details class="event-detail"><summary>Details</summary><pre>${esc(typeof detail==='string'?detail:JSON.stringify(detail,null,2))}</pre></details>`:''}</div>`;}
function renderApprovals(approvals){ if(!$('#approvals'))return;$('#approvals').innerHTML=approvals.filter(a=>a.status==='pending').map(a=>`<div class="approval"><h3>Approval needed: ${esc(a.tool)}</h3><pre>${esc(JSON.stringify(a.arguments,null,2))}</pre>${state.role==='admin'?`<div class="approval-actions"><button data-approval="${a.id}" data-decision="approve" class="primary small">Approve once</button><button data-approval="${a.id}" data-decision="deny" class="small">Deny</button></div>`:'<p>An organization administrator must approve this action.</p>'}</div>`).join('');document.querySelectorAll('[data-approval]').forEach(b=>b.onclick=async()=>{try{await api(`/api/approvals/${b.dataset.approval}`,{method:'POST',body:JSON.stringify({decision:b.dataset.decision})});await refreshApproval(state.selected);}catch(e){toast(e.message);}});}
async function refreshApproval(id){if(state.selected!==id)return;const run=await api(`/api/runs/${id}`);if(state.selected===id){renderApprovals(run.approvals);$('#run-status').innerHTML=statusLabel(run.status);}}
async function renderConnections(){
  const version=state.pageVersion;const [connections,organization]=await Promise.all([api('/api/connections'),api('/api/organization')]);if(version!==state.pageVersion)return;state.connections=connections;state.organization=organization;renderSidebar();
  const admin=state.role==='admin', org=state.organization, slack=org.slack_sessions;
  $('#content').innerHTML=`<div class="page-heading"><div><h1>Connections</h1><p class="subtext">Bring your team’s tools into every session.</p></div><span class="badge">${admin?'Administrator':'Member'}</span></div>
    <div class="org-summary"><div><strong>Shared with your organization</strong><p>Each session chooses which apps it uses. Credentials stay on the server.</p></div><span>${state.connections.filter(c=>c.connected).length} of ${state.connections.length} connected</span></div>
    <div class="connections-grid org-connections">${state.connections.map(c=>`<article class="connection-card"><div class="connection-top"><div class="app-icon ${c.id}">${providerNames[c.id][0]}</div><h2>${providerNames[c.id]}</h2><span class="connection-state ${c.connected&&c.enabled?'healthy':''}">${c.connected?(c.enabled?'Connected':'Paused'):'Not connected'}</span></div><p>${({linear:'Issues, requirements, and progress comments.',slack:'Conversation search and thread context.',notion:'Documentation, page context, and new notes.',github:'Read code and rulesets, maintain PRs, and update required reviewers when GitHub permissions allow.'})[c.id]}</p><dl class="connection-facts"><div><dt>Workspace</dt><dd>${c.id==='github'&&c.label?.length>80?`<details class="connection-repositories"><summary>View repositories</summary>${esc(c.label)}</details>`:esc(c.label||'—')}</dd></div><div><dt>Connection</dt><dd>${esc(c.identity)}</dd></div><div><dt>Access</dt><dd>${c.read_only?'Read only':c.id==='github'?'PRs and ruleset reviewers':'Read and write automatically'}</dd></div></dl><small>${c.check_status==='healthy'?'Verified '+relative(c.checked_at):c.check_status==='needs_attention'?'Connection needs attention':'Ready for a connection check'}</small><div class="connection-action"><button data-manage="${c.id}">${admin?'Manage':'View access'}</button></div></article>`).join('')}</div>
    <div class="org-lower"><section class="card org-slack"><div class="section-header"><h2>Start sessions from Slack</h2><span class="connection-state ${slack.enabled?'healthy':''}">${slack.enabled?'Enabled':'Setup needed'}</span></div><p>Mention <strong>@Moyai</strong> with a task in a channel the bot has joined. It reacts with 👀 when it picks up your message, then posts its answer in the thread. ${slack.thread_reply_ready?'Reply there to continue the same conversation; no repeat mention is needed.':'Mention the bot again in the same thread to continue. An administrator can reconnect Slack to enable replies without a mention.'}</p><p class="subtext">${slack.enabled?esc(slack.audience)+' can start sessions using enabled organization apps.':'The Slack workspace bot must be installed before mentions can start sessions.'} Everyone in that Slack thread can see the replies. Use stop, sleep, or wake to control the conversation. Enabled app tools run without an extra approval step.</p><p>${slack.direct_message_ready?'You can also message Moyai directly. Each new message starts a session. Reply in its thread to keep the same conversation and files.':'Direct messages need the bot’s DM access and message event subscription.'} DM sessions are also visible to signed-in BerriAI teammates in Moyai.</p>${slack.last_session?`<small>Latest request ${relative(slack.last_session.created_at)} · ${slack.last_session.reply_status==='sent'?'Reply sent':'Reply '+esc(slack.last_session.reply_status)}</small>`:''}</section>
    <section class="card org-access"><h2>Organization access</h2><p>Members can start sessions and use enabled app tools, including writes, without extra approval. Administrators manage connections and allowed actions.</p><p class="subtext">${org.google_signin?'Sign in with your BerriAI Google account. Administrators are assigned by email; other teammates join as members.':org.member_access_configured?'Separate administrator and member sign-ins are enabled.':'Currently using the shared administrator sign-in. Separate member access is not configured.'}</p>${admin?`<form id="org-name-form"><label for="org-name">Organization name</label><div class="inline-field"><input id="org-name" maxlength="80" required value="${esc(org.name)}"><button type="submit">Save</button></div></form>`:''}</section></div>
    <div class="note org-note"><strong>Shared connection, same provider identity</strong>A shared connection uses the account that authorized it. Sharing it here does not turn a personal account into a service account or expand its provider permissions. Conversational replies are posted automatically to the originating Slack conversation. All enabled app tools run without an extra approval step, including newly added tools and messages to other Slack conversations. Read-only and paused connection settings still apply.</div>
    ${org.activity.length?`<details class="org-history"><summary>Connection activity</summary>${org.activity.map(a=>`<div><span>${esc(providerNames[a.provider]||'Organization')} · ${esc(a.action)}</span><small>${esc(a.actor)} · ${relative(a.created_at)}</small></div>`).join('')}</details>`:''}`;
  document.querySelectorAll('[data-manage]').forEach(b=>b.onclick=()=>manageConnection(b.dataset.manage));
  if($('#org-name-form'))$('#org-name-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/organization',{method:'PATCH',body:JSON.stringify({name:$('#org-name').value})});await renderConnections();toast('Organization updated.');}catch(err){toast(err.message);}};
}
function manageConnection(provider){
  const c=state.connections.find(c=>c.id===provider), name=providerNames[provider], admin=state.role==='admin';
  $('#connection-title').textContent=name+' · Organization access';
  $('#connection-body').innerHTML=`<p>${c.connected?'Connected to '+esc(c.label)+' using '+esc(c.identity.toLowerCase())+'.':'No shared connection is installed.'}</p><ul class="tool-list">${c.tools.map(t=>`<li>${esc(t.description)}</li>`).join('')}</ul>${admin?`<label class="policy-check"><input id="connection-enabled" type="checkbox" ${c.enabled?'checked':''}>Available to organization sessions</label><div class="field"><label for="connection-access">Allowed actions</label><select id="connection-access"><option value="write" ${!c.read_only?'selected':''}>${provider==='github'?'Pull requests and ruleset reviewers':'Read and write automatically'}</option><option value="read" ${c.read_only?'selected':''}>Read only</option></select></div><p class="subtext">Pausing access or switching to read only blocks new writes. An action already sent cannot be recalled.</p><button class="primary full" type="submit">Save access</button><div class="connection-controls">${c.connected?'<button id="connection-check" type="button">Check connection</button>':''}${c.connected&&provider==='github'?'<button id="github-repositories" type="button">Choose repositories</button>':''}<button id="connection-reconnect" type="button">${c.connected?'Reconnect':'Connect'} ${name}</button>${c.connected?'<button id="connection-disconnect" class="danger" type="button">Disconnect</button>':''}</div>`:`<div class="note">${c.enabled?'Available to sessions.':'Paused for all sessions.'} ${c.read_only?'Read only.':provider==='github'?'PR creation, session-owned PR updates, comments and requested ruleset reviewer edits execute directly. Ruleset edits require GitHub Administration write access. PR approval and merging are blocked.':'Enabled tools run automatically, including writes. No administrator approval step is required.'} Ask an organization administrator to change this connection.</div>`}<div id="connection-error" role="alert"></div>`;
  $('#connection-form').onsubmit=async e=>{e.preventDefault();if(!admin)return;try{await api('/api/connections/'+provider+'/policy',{method:'PATCH',body:JSON.stringify({enabled:$('#connection-enabled').checked,read_only:$('#connection-access').value==='read'})});$('#connection-dialog').close();await renderConnections();toast(name+' access updated.');}catch(error){$('#connection-error').textContent=error.message;}};
  if($('#github-repositories'))$('#github-repositories').onclick=githubRepositoriesDialog;
  if($('#connection-reconnect'))$('#connection-reconnect').onclick=()=>{ $('#connection-dialog').close();connectionDialog(provider); };
  if($('#connection-check'))$('#connection-check').onclick=async()=>{const b=$('#connection-check');b.disabled=true;try{await api('/api/connections/'+provider+'/check',{method:'POST'});toast(name+' connection verified.');await renderConnections();}catch(error){$('#connection-error').textContent=error.message;}finally{b.disabled=false;}};
  if($('#connection-disconnect'))$('#connection-disconnect').onclick=async()=>{const b=$('#connection-disconnect');if(b.dataset.confirm!=='yes'){b.dataset.confirm='yes';b.textContent='Disconnect for everyone';return;}try{await api('/api/connections/'+provider,{method:'DELETE'});$('#connection-dialog').close();await renderConnections();toast(name+' disconnected.');}catch(error){$('#connection-error').textContent=error.message;}};
  $('#connection-dialog').showModal();
}
async function githubRepositoriesDialog(){
  $('#connection-title').textContent='GitHub · Repositories';
  $('#connection-body').innerHTML='<p>Loading repositories…</p><div id="connection-error" role="alert"></div>';
  $('#connection-form').onsubmit=e=>e.preventDefault();
  try{
    const data=await api('/api/connections/github/repositories');
    $('#connection-body').innerHTML=`<p>Choose the repositories Moyai can use. Renaming a repository keeps its connection and saved work.</p><div class="github-repository-options">${data.repositories.map(repo=>`<label class="policy-check"><input type="checkbox" name="repository-id" value="${repo.id}" ${data.selected_ids.includes(repo.id)?'checked':''}>${esc(repo.full_name)}</label>`).join('')}</div><p class="subtext">Missing a repository? <a href="${esc(data.installation_url)}" target="_blank" rel="noopener noreferrer">Manage the GitHub App’s access</a>, then refresh this list.</p><button id="github-repository-refresh" type="button">Refresh list</button><button class="primary full" type="submit">Save repositories</button><div id="connection-error" role="alert"></div>`;
    $('#github-repository-refresh').onclick=githubRepositoriesDialog;
    $('#connection-form').onsubmit=async e=>{
      e.preventDefault();const button=e.currentTarget.querySelector('[type="submit"]');button.disabled=true;
      try{
        const repository_ids=[...document.querySelectorAll('[name="repository-id"]:checked')].map(input=>Number(input.value));
        if(!repository_ids.length)throw new Error('Select at least one repository. Use Disconnect to remove all GitHub access.');
        await api('/api/connections/github/repositories',{method:'POST',body:JSON.stringify({repository_ids})});
        $('#connection-dialog').close();await renderConnections();toast('GitHub repositories updated.');
      }catch(error){$('#connection-error').textContent=error.message;button.disabled=false;}
    };
  }catch(error){$('#connection-error').textContent=error.message;}
}
function connectionDialog(provider){
  const connection=state.connections.find(c=>c.id===provider),name=providerNames[provider];
  const hints={linear:'Authorize the Linear account or integration your organization should use. Its existing team permissions still apply.',slack:'Connect the Slack account whose conversations your organization can search. The workspace bot handles session mentions separately.',notion:'Connect Notion and choose the pages your organization can use in sessions.'};
  $('#connection-title').textContent='Connect '+name+' for your organization';
  if(provider==='github'){
    const repos=connection.repositories||[];
    $('#connection-body').innerHTML=`<p>Connect once for your team. Choose repositories in Moyai after installing your organization’s GitHub App.</p><ul>${repos.map(repo=>`<li><strong>${esc(repo.full_name)}</strong></li>`).join('')}</ul><p>Moyai can read code, open normal pull requests, and update or comment on PRs created by the current session without an administrator approval step. It cannot approve or merge PRs, enable auto-merge, or change workflows and access controls.</p>${connection.app_registered?'<button class="primary full" type="submit">Continue with GitHub</button>':`<h3>Connect an existing GitHub App</h3><p>An organization App owner or manager can find the App ID and download a private key in GitHub’s App settings. Upload it here, never in chat. The key stays encrypted on Moyai’s server.</p><div class="field"><label for="github-app-id">GitHub App ID</label><input id="github-app-id" type="number" min="1" required autocomplete="off"></div><div class="field"><label for="github-app-key">Private key (.pem)</label><input id="github-app-key" type="file" accept=".pem" required><small>Used only to authenticate your organization’s GitHub App.</small></div><button class="primary full" type="submit">Verify app and continue</button><div class="divider">or</div><div class="field"><label for="github-organization">GitHub organization</label><input id="github-organization" placeholder="Your organization" autocomplete="off"></div><button class="quiet full" id="github-new-app" type="button">Register a new GitHub App</button>`}<div id="connection-error" role="alert"></div>`;
    async function githubSetup(existing){
      const buttons=document.querySelectorAll('#connection-form button');buttons.forEach(button=>button.disabled=true);
      try{
        let path='/api/connections/github/oauth',body;
        if(existing){
          const file=$('#github-app-key').files[0];
          if(!file||file.size>20000)throw new Error('Choose a PEM private key file under 20 KB.');
          path='/api/connections/github/app';
          body=JSON.stringify({app_id:Number($('#github-app-id').value),private_key:await file.text()});
        }
        if(!existing&&!connection.app_registered)body=JSON.stringify({organization:$('#github-organization').value.trim()});
        const result=await api(path,{method:'POST',...(body?{body}:{})});
        if($('#github-app-key'))$('#github-app-key').value='';
        if(result.connected){$('#connection-dialog').close();await renderConnections();toast('GitHub refreshed.');}
        else location.assign(result.url);
      }catch(error){$('#connection-error').textContent=error.message;buttons.forEach(button=>button.disabled=false);}
    }
    $('#connection-form').onsubmit=async e=>{e.preventDefault();await githubSetup(!connection.app_registered);};
    if($('#github-new-app'))$('#github-new-app').onclick=()=>githubSetup(false);
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
async function renderRuntime(){const version=state.pageVersion;const config=await api('/api/config');if(version!==state.pageVersion)return;state.config=config;const fields=['MODAL_TOKEN_ID','MODAL_TOKEN_SECRET','LITELLM_API_BASE','LITELLM_API_KEY','AGENT_MODEL',...state.config.missing.filter(x=>x.startsWith('PUBLIC_URL'))];$('#content').innerHTML=`<div class="page-heading"><div><h1>Runtime</h1><p class="subtext">Cloud readiness, session limits, and recovery.</p></div><span class="badge">${state.config.cloud_ready?'Cloud ready':'Setup needed'}</span></div><div class="setup-grid"><div class="card"><h2>Modal + ${esc((config.harnesses||[]).find(h=>h.id===config.harness)?.name||config.harness)}</h2><p class="subtext">Modal provides the machine. The selected agent plans the work, uses tools, and returns the result.</p><ul class="config-list">${fields.map(key=>`<li><code>${key}</code><span class="${state.config.missing.includes(key)?'missing':'configured'}">${state.config.missing.includes(key)?'Not configured':'Configured'}</span></li>`).join('')}</ul></div><div class="card"><h2>Workspace settings</h2><div class="detail-row"><span>Session recovery</span><span>${esc(state.config.execution_engine||"Local worker")}${state.config.execution_connected===false?" · reconnecting":""}</span></div>${state.config.checkpoint_interval_seconds?`<div class="detail-row"><span>Save progress</span><span>Every ${Math.round(state.config.checkpoint_interval_seconds/60)} minutes between tool rounds</span></div>`:""}<div class="detail-row"><span>Maximum active + idle sandboxes</span><span>${state.config.max_concurrent_runs}</span></div><div class="detail-row"><span>Parallel agents</span><span>${state.config.parallel_agents_enabled?'Up to '+state.config.max_parallel_agents+' workers per group':'Requires Temporal'}</span></div><div class="detail-row"><span>Idle sessions</span><span>${state.config.sandbox_idle_seconds?Math.round(state.config.sandbox_idle_seconds/60)+' minutes of sandbox reuse':'No running sandbox'}</span></div><div class="detail-row"><span>Response time limit</span><span>${state.config.run_timeout_seconds?Math.round(state.config.run_timeout_seconds/60)+' minutes':'No app limit'}</span></div><div class="detail-row"><span>Model</span><span>${esc(state.config.model||'Choose in .env')}</span></div><p class="subtext" style="margin-top:25px">Your administrator manages the cloud connection. Each response runs in an isolated workspace and saves its files for the next message. Long tasks automatically continue on a fresh machine before Modal’s 24-hour limit.${state.config.sandbox_idle_seconds?' Idle sandboxes can be released sooner when another session needs capacity. Finished workers and coordinators waiting for workers release immediately.':''}</p></div></div>`;}
function showError(error){toast(error.message);}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>navigate(b.dataset.view).catch(showError));
$('#new-task').onclick=()=>navigate('tasks').then(()=>$('#prompt')?.focus()).catch(showError);
$('#new-folder').onclick=()=>editSessionFolder();
$('#session-list').onclick=e=>{const folderToggle=e.target.closest('[data-toggle-folder]');if(folderToggle){toggleSessionFolder(folderToggle.dataset.toggleFolder);return;}const folderEdit=e.target.closest('[data-edit-folder]');if(folderEdit){const folder=state.folders.find(f=>f.id===folderEdit.dataset.editFolder);if(folder)editSessionFolder(folder);return;}const move=e.target.closest('[data-move-session]');if(move){const run=state.runs.find(r=>r.id===move.dataset.moveSession);if(run)moveSessionToFolder(run);return;}const toggle=e.target.closest('[data-toggle-agents]');if(toggle){const id=toggle.dataset.toggleAgents;if(state.expandedParents.has(id))state.expandedParents.delete(id);else state.expandedParents.add(id);renderSidebar();return;}const button=e.target.closest('[data-run]');if(button)openRun(button.dataset.run).catch(showError);};
bindSessionFolderDragDrop($('#session-list'));
$('#session-scope').onchange=changeSessionScope;
$('#search-sessions').setAttribute('aria-controls','session-search-field');
$('#search-sessions').setAttribute('aria-expanded','false');
$('#session-search').oninput=renderSidebar;
$('#session-search').onkeydown=e=>{if(e.key==='Escape'){e.stopPropagation();$('#session-search').value='';renderSidebar();closeSessionSearch();$('#search-sessions').focus();}};
const wideRail=()=>!matchMedia('(max-width:850px)').matches;$('#open-sidebar').onclick=()=>{if(wideRail())document.body.classList.remove('rail-collapsed');else setSidebar(true);};$('#search-sessions').onclick=()=>{if(!wideRail())setSidebar(true);openSessionSearch();};$('#session-search').onblur=closeSessionSearch;$('#close-sidebar').onclick=()=>{if(wideRail()){document.body.classList.add('rail-collapsed');$('#open-sidebar').focus();}else setSidebar(false);};$('#sidebar-scrim').onclick=()=>setSidebar(false);
window.addEventListener('keydown',e=>{if(e.key==='Escape'){setSidebar(false);if(state.selected&&$('.chat-layout'))toggleDetails(false);}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'&&state.csrf){e.preventDefault();$('#new-task').click();}});
$('.dialog-close').onclick=()=>$('#connection-dialog').close();
window.addEventListener('hashchange',()=>{
  if(!state.csrf)return;
  const linkedRun=location.hash.match(/^#run=([a-f0-9]{32})$/)?.[1];
  (linkedRun?openRun(linkedRun):navigate(settingsViews.has(location.hash.slice(1))?location.hash.slice(1):'tasks')).catch(showError);
});
document.querySelector('.skip-link')?.addEventListener('click',event=>{event.preventDefault();$('#content').focus();});
async function boot(){
  try{
    const session=await api('/api/session');applyUserSession(session);restoreSessionFolderView();
    $('.rail-foot small').textContent=session.local?'Private · local preview':'Shared internal workspace';
    if(!session.authenticated){
      const passwordForm='<form id="login-form"><div class="field"><label for="password">Workspace password</label><input id="password" type="password" autocomplete="current-password" required></div><button class="primary full">Sign in</button></form>';
      $('#content').innerHTML=`<div class="login"><div class="card"><div class="login-mark" aria-hidden="true">M</div><h1>Welcome to Moyai</h1><p class="subtext">${session.google_enabled?'Your team’s agent, one conversation away.':'Sign in to Moyai.'}</p>${session.google_enabled?`<button id="google-signin" class="google-signin full" type="button"><span class="google-letter" aria-hidden="true">G</span> Continue with Google</button><p class="login-domain">Use your ${session.google_domains.map(d=>'@'+esc(d)).join(' or ')} work account.</p>`:''}${session.password_enabled?(session.google_enabled?'<details class="password-fallback"><summary>Use workspace password</summary>'+passwordForm+'</details>':passwordForm):''}<p id="signin-error" class="signin-error" role="alert"></p></div></div>`;
      if($('#login-form'))$('#login-form').onsubmit=async(e)=>{e.preventDefault();try{await api('/api/login',{method:'POST',body:JSON.stringify({password:$('#password').value})});await boot();}catch(error){toast(error.message);}};
      if($('#google-signin'))$('#google-signin').onclick=async()=>{const button=$('#google-signin');button.disabled=true;try{const result=await api('/api/auth/google/start',{method:'POST',body:JSON.stringify({return_to:'/'+location.hash})});location.assign(result.url);}catch(error){button.disabled=false;$('#signin-error').textContent=error.message;}};
      const signInError=new URLSearchParams(location.search).get('signin');
      if(signInError){$('#signin-error').textContent=signInError==='cancelled'?'Sign-in was cancelled. You can try again.':'We couldn’t sign you in. Use your BerriAI Google Workspace account and try again.';history.replaceState(null,'','/'+location.hash);}
      document.querySelectorAll('.rail button').forEach(b=>b.disabled=true);
      return;
    }
    document.querySelectorAll('.rail button').forEach(b=>b.disabled=false);
    [state.config,state.organization]=await Promise.all([api('/api/config'),api('/api/organization')]);await refreshRuns();
    $('#logout').hidden=!!session.local;
    if(!session.local){$('.rail-foot small').textContent=session.identity?session.identity.email:state.role==='admin'?'Organization admin':'Organization member';$('.rail-foot small').title=state.role==='admin'?'Organization admin':'Organization member';}
    $('#logout').onclick=async()=>{const button=$('#logout');button.disabled=true;try{await api('/api/logout',{method:'POST'});location.reload();}catch(error){button.disabled=false;showError(error);}};
    const linkedRun=location.hash.match(/^#run=([a-f0-9]{32})$/)?.[1]; if(linkedRun)await openRun(linkedRun);else await navigate(settingsViews.has(location.hash.slice(1))?location.hash.slice(1):'tasks');
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
document.addEventListener('change',e=>{const picker=e.target.closest?.('.model-picker select'),logo=picker?.parentElement.querySelector('.provider-logo');if(logo)MoyaiProviderLogos.sync(logo,picker.value,picker.id==='new-harness'?MoyaiProviderLogos.harness:MoyaiProviderLogos.src);});
setInterval(()=>{if(state.authenticated&&!document.hidden)refreshRuns().catch(()=>{});},15000);
boot();

setInterval(()=>{if(state.authenticated&&!document.hidden&&state.selected&&state.runs.find(r=>r.id===state.selected)?.children?.some(c=>!terminal.has(c.status)))refreshChat(state.selected).catch(()=>{});},10000);

// Only elapsed labels change between actual events; drafts and expanded rows stay put.
setInterval(()=>{MoyaiActivity.tick($('#conversation'));MoyaiActivity.tick($('#activity-history'));if(state.chatRun)MoyaiGoal.render($('#goal-status'),state.chatRun,esc);},1000);
