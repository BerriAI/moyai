const spendState={start:'',end:'',user:'',timer:null,version:0,displayed:'',scope:''};
function dollars(value){return new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:6}).format(Number(value));}
function spendCount(value){return Number(value||0).toLocaleString();}
function spendCostSource(row){return {response_header:'Response header',response_usage:'Response usage',gateway_recovery:'Recovered receipt'}[row.cost_source]||(row.cost!==null?'Earlier record':'Not returned');}
function spendCostValue(row){return row.cost!==null?'$'+esc(row.cost):row.cost_status==='pending'||row.status==='pending'?'Cost pending':row.cost_recovery_error==='receipt_access_denied'?'Receipt access required':'Unknown';}
function slackIdentityStatus(user){
  return {linked:'Linked automatically',manual:'Linked by administrator',awaiting_google:'Profile created · awaiting first Google sign-in',pending_profile:'Profile lookup pending',unavailable:'Profile unavailable · retrying',ineligible:'Not eligible for automatic matching',email_changed:'Email changed · administrator review needed',review:'Administrator review needed'}[user.link_status]||'Profile lookup pending';
}
function renderSlackIdentities(slack,google,status){
  const message=!status.enabled?'Automatic matching is disabled.':status.ready?'Automatic matching is on. Slack profiles are created on first use and linked when the same email signs in with Google.':status.missing_scopes.length?'Reconnect Slack to allow profile and email lookup. Existing links still apply.':'Enable the Slack connection to look up profiles.';
  return `<section class="card spend-section" id="slack-identities"><div class="section-header"><h2>Slack identities</h2><button id="refresh-identities" ${status.ready?'':'disabled'}>Refresh profiles</button></div><p class="subtext">${message} Google sign-in is still required to access the web app.</p>${!status.ready&&status.enabled?'<a href="#connections">Open organization connections →</a>':''}${slack.length?`<div class="spend-table-wrap" role="region" aria-label="Slack identity accounts" tabindex="0"><table class="spend-table"><thead><tr><th>Slack teammate</th><th>Account status</th><th>Google account</th></tr></thead><tbody>${slack.map(u=>`<tr><td><strong>${esc(u.name)}</strong><small>${esc(u.email||u.id)}</small></td><td>${esc(slackIdentityStatus(u))}</td><td>${esc(google.find(g=>g.id===u.linked_user_id)?.email||'Awaiting match')}</td></tr>`).join('')}</tbody></table></div><details class="identity-overrides"><summary>Administrator overrides</summary><p class="subtext">Use a manual link to resolve an exception. A teammate must sign in with Google before you can select their account. Original sender records are preserved.</p>${slack.map(u=>`<form class="spend-link-form" data-slack="${esc(u.id)}"><span>${esc(u.email||u.name)}</span><label><span class="sr-only">Google account for ${esc(u.name)}</span><select name="google" required><option value="">Choose Google account</option>${google.map(g=>`<option value="${esc(g.id)}" ${u.linked_user_id===g.id?'selected':''}>${esc(g.email)}</option>`).join('')}</select></label><button type="submit" ${google.length?'':'disabled'}>Save manual link</button></form>`).join('')}</details>`:'<p class="subtext">Teammates will appear here when they use Moyai in Slack.</p>'}</section>`;
}
function renderPersonalSpendSummary(data){
  return `<div class="spend-cards cost-summary">
    <section class="card cost-total"><span>Your LLM spend</span><strong>${dollars(data.total.spend)}</strong><small>${spendCount(data.priced_requests)} of ${spendCount(data.total.requests)} requests priced</small></section>
    <section class="card"><span>Your requests</span><strong>${spendCount(data.total.requests)}</strong><small>Across ${spendCount(data.total.sessions)} sessions</small></section>
    <section class="card"><span>Your tokens</span><strong>${spendCount(data.total.total_tokens)}</strong><small>${spendCount(data.total.prompt_tokens)} input · ${spendCount(data.total.completion_tokens)} output</small></section>
  </div>`;
}
function renderSpendFilters(start,end){
  return `<form class="spend-filters" id="spend-filter-form"><label>From<input type="date" id="spend-start" required value="${esc(start)}"></label><label>Through<input type="date" id="spend-end" required value="${esc(end)}"></label><button type="submit">Apply dates</button><button type="button" id="sync-spend">Refresh</button><span class="subtext">Dates in UTC · up to 93 days</span></form>`;
}
function bindSpendFilters(){
  $('#spend-filter-form').onsubmit=e=>{e.preventDefault();if($('#spend-range'))$('#spend-range').open=false;$('#spend-range-toggle')?.focus?.();spendState.start=$('#spend-start').value;spendState.end=$('#spend-end').value;renderSpend().catch(showError);};
  if(typeof bindAnalyticsPreset==='function')bindAnalyticsPreset('spend',range=>{Object.assign(spendState,range);renderSpend().catch(showError);});
  $('#sync-spend').onclick=async()=>{const button=$('#sync-spend');button.disabled=true;button.textContent='Refreshing…';try{await renderSpend();}catch(e){showError(e);button.disabled=false;button.textContent='Refresh';}};
}
function spendContext(start=spendState.start,end=spendState.end){return [state.pageVersion,state.role,start,end].join('/');}
function preserveSpendView(){
  const root=$('#content'),active=document.activeElement;
  if(typeof document.querySelectorAll!=='function')return ()=>{};
  const nodes=[...document.querySelectorAll('#content [id]')].map(el=>({id:el.id,open:el.open,value:el.matches?.('input,select,textarea')?el.value:undefined,top:el.scrollTop,left:el.scrollLeft}));
  const details=[...document.querySelectorAll('#content details')].map(el=>({label:el.querySelector('summary')?.textContent,open:el.open}));
  const scrollers=[...document.querySelectorAll('#content .spend-table-wrap')].map(el=>({label:el.getAttribute('aria-label'),left:el.scrollLeft,top:el.scrollTop}));
  const focus=active?.id,start=active?.selectionStart,end=active?.selectionEnd,top=root.scrollTop;
  return ()=>{
    for(const saved of nodes){const el=document.getElementById?.(saved.id);if(!el)continue;if(saved.open!==undefined)el.open=saved.open;if(saved.value!==undefined)el.value=saved.value;el.scrollTop=saved.top;el.scrollLeft=saved.left;}
    for(const el of document.querySelectorAll('#content details')){const saved=details.find(s=>s.label===el.querySelector('summary')?.textContent);if(saved)el.open=saved.open;}
    for(const el of document.querySelectorAll('#content .spend-table-wrap')){const saved=scrollers.find(s=>s.label===el.getAttribute('aria-label'));if(saved){el.scrollTop=saved.top;el.scrollLeft=saved.left;}}
    const el=focus?document.getElementById?.(focus):null;el?.focus?.({preventScroll:true});if(start!=null)el?.setSelectionRange?.(start,end);
    root.scrollTop=top;
  };
}
function spendRefreshNotice(message='',error=false){
  const notice=$('#spend-refresh-status');
  if(notice){notice.className=error?'error-banner':'subtext';notice.textContent=message==='Updating…'?'':message;}
  const button=$('#sync-spend');if(button){button.disabled=false;button.textContent=message==='Updating…'?'Refreshing…':'Refresh';}
}
async function renderSpend(background = false){
  clearTimeout(spendState.timer);
  if(state.view!=='spend')return;
  if(background && (document.hidden||settingsInteractionActive())){
    spendState.timer=setTimeout(()=>renderSpend(true).catch(showError),5000);
    return;
  }
  const version=state.pageVersion,renderVersion=++spendState.version;
  const current=()=>version===state.pageVersion&&renderVersion===spendState.version;
  const context=spendContext(),retained=spendState.displayed===context;
  clearTimeout(spendPRState.timer);
  if(retained)spendRefreshNotice('Updating…');
  else {spendState.displayed='';spendPRState.key='';spendPRState.data=null;spendPRState.promise=null;$('#content').innerHTML='<p class="subtext" role="status">Loading spend…</p>';}
  const query=new URLSearchParams();if(spendState.start)query.set('start',spendState.start);if(spendState.end)query.set('end',spendState.end);
  let data,identityStatus;
  try{
    data=await api('/api/spend?'+query);
    if(!current())return;
    if(retained&&data.scope!==spendState.scope){spendState.displayed='';$('#content').innerHTML='<p class="subtext" role="status">Loading spend…</p>';}
    if(data.scope!=='organization'){clearTimeout(spendPRState.timer);spendPRState.key='';spendPRState.data=null;spendPRState.promise=null;}
    identityStatus=data.scope==='organization'?await api('/api/admin/identities/status'):null;
  }catch(error){
    if(!current())return;
    if(retained&&spendState.displayed===context&&error.status!==401&&error.status!==403){spendRefreshNotice('Could not update. '+error.message+' Use Refresh to retry.',true);return;}
    spendState.displayed='';spendPRState.key='';spendPRState.data=null;spendPRState.promise=null;
    $('#content').innerHTML=`<div class="page-heading"><h1>${state.role==='admin'?'Spend & usage':'Spend'}</h1></div><div class="error-banner" role="alert">${esc(error.message)}</div>${renderSpendFilters(spendState.start,spendState.end)}`;
    bindSpendFilters();
    return;
  }
  if(!current())return;
  if(background&&data.scope===spendState.scope&&(document.hidden||settingsInteractionActive())){
    spendRefreshNotice();
    spendState.timer=setTimeout(()=>{if(current())renderSpend(true).catch(showError);},5000);
    return;
  }
  const restore=retained&&data.scope===spendState.scope?preserveSpendView():()=>{};
  const admin=data.scope==='organization';spendState.scope=data.scope;
  spendState.start=data.start;spendState.end=data.end;
  if(admin){
    const rendered=renderAdminSpend(data,identityStatus,null,true);
    spendState.displayed=spendContext(data.start,data.end);restore();
    await rendered;if(!current())return;
    if(data.infrastructure.pending||data.total.pending_costs)spendState.timer=setTimeout(()=>{if(current())renderSpend(true).catch(showError);},5000);
    return;
  }
  spendState.user='';
  const pending=data.total.pending_costs,missing=data.total.missing_costs;
  const verified=data.total.requests>0&&!pending&&!missing;
  const sessions=data.sessions;
  $('#content').innerHTML=`<div class="page-heading"><div><h1>Your LLM spend</h1><p class="subtext">Only your model usage and costs, including your linked Slack activity.</p></div><span class="badge">USD</span></div>
    ${renderSpendFilters(data.start,data.end)}<div id="spend-refresh-status" role="status"></div>
    ${renderPersonalSpendSummary(data)}
    <div class="spend-reconciliation ${verified?'verified':''}" role="status"><strong>${verified?'✓ All tracked LLM requests priced':missing?'Some costs are unavailable':data.total.requests?'Costs pending':'Ready to track usage'}</strong><p>${verified?'Your total includes the costs returned by the gateway for your requests.':data.total.requests?`${spendCount(pending)} costs await confirmation; ${spendCount(missing)} costs are unavailable. Missing costs are not treated as free.`:'New model requests will be attributed to the signed-in user using the cost returned by LiteLLM.'}</p></div>
    <section class="card spend-section"><h2>Your sessions</h2><div class="spend-table-wrap" role="region" aria-label="Session spend" tabindex="0"><table class="spend-table"><thead><tr><th>Session</th><th>Requests</th><th>Spend</th></tr></thead><tbody>${sessions.map(s=>`<tr><td><a href="#run=${esc(s.run_id)}">${esc(s.title)}</a></td><td>${spendCount(s.requests)}</td><td>${dollars(s.spend)}${s.missing_costs?'<small>Cost missing</small>':s.pending_costs?'<small>Cost pending</small>':''}</td></tr>`).join('')||`<tr><td colspan="3" class="subtext">No model requests attributed to you in this period.</td></tr>`}</tbody></table></div></section>
    <section class="card spend-section"><h2>Your models</h2><div class="spend-models">${data.models.map(m=>`<div><span>${esc(modelName(m.model))}</span><strong>${dollars(m.spend)}</strong><small>${spendCount(m.requests)} requests</small></div>`).join('')||'<p class="subtext">No usage yet.</p>'}</div></section>
    <details class="card spend-section"><summary>Request costs · latest 500 in this period</summary><div class="spend-table-wrap" role="region" aria-label="Exact request costs" tabindex="0"><table class="spend-table"><thead><tr><th>Time</th><th>Model</th><th>Cache read tokens</th><th>Cache write tokens</th><th>Cost source</th><th>Exact USD</th></tr></thead><tbody>${data.request_details.map(r=>`<tr><td><a href="#run=${esc(r.run_id)}" title="${esc(r.id)}">${esc(new Date(r.created_at).toLocaleString())}</a></td><td>${esc(modelName(r.model))}</td><td>${r.cache_read_input_tokens==null?'Not reported':spendCount(r.cache_read_input_tokens)}</td><td>${r.cache_creation_input_tokens==null?'Not reported':spendCount(r.cache_creation_input_tokens)}</td><td>${spendCostSource(r)}</td><td>${spendCostValue(r)}</td></tr>`).join('')||'<tr><td colspan="6">No requests in this period.</td></tr>'}</tbody></table></div></details>
    <p class="subtext spend-note">LLM costs are saved from inference responses and recovered gateway receipts. Recorded costs are preserved across gateway key rotations. Each response is attributed to the person who sent that message. Your view excludes other users and infrastructure costs. Calls made with separate credential-proxy keys are not included. Usage before tracking began or outside Moyai is not included. An interrupted or failed request may have no returned cost, so this is not a full audit of the key’s lifetime spend. ${data.tracked_since?'Per-user tracking began '+esc(new Date(data.tracked_since).toLocaleString())+'.':''}</p>`;
  bindSpendFilters();spendState.displayed=spendContext(data.start,data.end);restore();
  if(pending)spendState.timer=setTimeout(()=>{if(current())renderSpend(true).catch(showError);},5000);
}
