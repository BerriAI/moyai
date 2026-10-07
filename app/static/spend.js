const spendState={start:'',end:'',user:'',timer:null,version:0};
function dollars(value){return new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:6}).format(Number(value));}
function spendCount(value){return Number(value||0).toLocaleString();}
function slackIdentityStatus(user){
  return {linked:'Linked automatically',manual:'Linked by administrator',awaiting_google:'Profile created · awaiting first Google sign-in',pending_profile:'Profile lookup pending',unavailable:'Profile unavailable · retrying',ineligible:'Not eligible for automatic matching',email_changed:'Email changed · administrator review needed',review:'Administrator review needed'}[user.link_status]||'Profile lookup pending';
}
function renderSlackIdentities(slack,google,status){
  const message=!status.enabled?'Automatic matching is disabled.':status.ready?'Automatic matching is on. Slack profiles are created on first use and linked when the same email signs in with Google.':status.missing_scopes.length?'Reconnect Slack to allow profile and email lookup. Existing links still apply.':'Enable the Slack connection to look up profiles.';
  return `<section class="card spend-section" id="slack-identities"><div class="section-header"><h2>Slack identities</h2><button id="refresh-identities" ${status.ready?'':'disabled'}>Refresh profiles</button></div><p class="subtext">${message} Google sign-in is still required to access the web app.</p>${!status.ready&&status.enabled?'<a href="#connections">Open organization connections →</a>':''}${slack.length?`<div class="spend-table-wrap"><table class="spend-table"><thead><tr><th>Slack teammate</th><th>Account status</th><th>Google account</th></tr></thead><tbody>${slack.map(u=>`<tr><td><strong>${esc(u.name)}</strong><small>${esc(u.email||u.id)}</small></td><td>${esc(slackIdentityStatus(u))}</td><td>${esc(google.find(g=>g.id===u.linked_user_id)?.email||'Awaiting match')}</td></tr>`).join('')}</tbody></table></div><details class="identity-overrides"><summary>Administrator overrides</summary><p class="subtext">Use a manual link to resolve an exception. A teammate must sign in with Google before you can select their account. Original sender records are preserved.</p>${slack.map(u=>`<form class="spend-link-form" data-slack="${esc(u.id)}"><span>${esc(u.email||u.name)}</span><label><span class="sr-only">Google account for ${esc(u.name)}</span><select name="google" required><option value="">Choose Google account</option>${google.map(g=>`<option value="${esc(g.id)}" ${u.linked_user_id===g.id?'selected':''}>${esc(g.email)}</option>`).join('')}</select></label><button type="submit" ${google.length?'':'disabled'}>Save manual link</button></form>`).join('')}</details>`:'<p class="subtext">Teammates will appear here when they use Moyai in Slack.</p>'}</section>`;
}
function renderPersonalSpendSummary(data){
  return `<div class="spend-cards cost-summary">
    <section class="card cost-total"><span>Your LLM spend</span><strong>${dollars(data.total.spend)}</strong><small>${spendCount(data.priced_requests)} of ${spendCount(data.total.requests)} requests priced</small></section>
    <section class="card"><span>Your requests</span><strong>${spendCount(data.total.requests)}</strong><small>Across ${spendCount(data.total.sessions)} sessions</small></section>
    <section class="card"><span>Your tokens</span><strong>${spendCount(data.total.total_tokens)}</strong><small>${spendCount(data.total.prompt_tokens)} input · ${spendCount(data.total.completion_tokens)} output</small></section>
  </div>`;
}
function renderSpendFilters(start,end){
  return `<form class="spend-filters" id="spend-filter-form"><label>From<input type="date" id="spend-start" required value="${esc(start)}"></label><label>Through<input type="date" id="spend-end" required value="${esc(end)}"></label><button type="submit">Apply dates</button><button type="button" id="sync-spend" class="primary">Refresh</button><span class="subtext">Dates in UTC · up to 93 days</span></form>`;
}
function bindSpendFilters(){
  $('#spend-filter-form').onsubmit=e=>{e.preventDefault();spendState.start=$('#spend-start').value;spendState.end=$('#spend-end').value;renderSpend().catch(showError);};
  $('#sync-spend').onclick=async()=>{const button=$('#sync-spend');button.disabled=true;button.textContent='Refreshing…';try{await renderSpend();}catch(e){showError(e);button.disabled=false;button.textContent='Refresh';}};
}
async function renderSpend(){
  clearTimeout(spendState.timer);
  const version=state.pageVersion,renderVersion=++spendState.version;
  const current=()=>version===state.pageVersion&&renderVersion===spendState.version;
  $('#content').innerHTML='<p class="subtext" role="status">Loading spend…</p>';
  const query=new URLSearchParams();if(spendState.start)query.set('start',spendState.start);if(spendState.end)query.set('end',spendState.end);
  let data,identityStatus;
  try{
    data=await api('/api/spend?'+query);
    if(!current())return;
    identityStatus=data.scope==='organization'?await api('/api/admin/identities/status'):null;
  }catch(error){
    if(!current())return;
    $('#content').innerHTML=`<div class="error-banner" role="alert">${esc(error.message)}</div>${renderSpendFilters(spendState.start,spendState.end)}`;
    bindSpendFilters();
    return;
  }
  if(!current())return;
  const admin=data.scope==='organization';
  spendState.start=data.start;spendState.end=data.end;
  if(!admin)spendState.user='';
  const pending=data.total.pending_costs,missing=data.total.missing_costs;
  const verified=data.total.requests>0&&!pending&&!missing;
  const identities=data.identities,google=identities.filter(u=>u.kind==='google'),slack=identities.filter(u=>u.kind==='slack');
  const users=spendState.user?data.users.filter(u=>u.id===spendState.user):data.users;
  const sessions=data.sessions.filter(s=>!spendState.user||s.user_id===spendState.user);
  $('#content').innerHTML=`<div class="page-heading"><div><div class="eyebrow">${admin?'ORGANIZATION / ADMIN':'PERSONAL USAGE'}</div><h1>${admin?'Usage & spend':'Your LLM spend'}</h1><p class="subtext">${admin?'Everything it costs to run Moyai, from infrastructure to LLMs.':'Only your model usage and costs, including your linked Slack activity.'}</p></div><span class="badge">USD</span></div>
    ${renderSpendFilters(data.start,data.end)}
    ${admin?renderCostSummary(data):renderPersonalSpendSummary(data)}
    ${admin?renderInfrastructure(data):''}
    <div class="spend-reconciliation ${verified?'verified':''}" role="status"><strong>${verified?'✓ All tracked LLM requests priced':missing?'Some costs are unavailable':data.total.requests?'Requests in progress':'Ready to track usage'}</strong><p>${verified?(admin?'User totals add up to the costs returned by your gateway for these requests.':'Your total includes the costs returned by the gateway for your requests.'):data.total.requests?`${spendCount(pending)} requests are in progress; ${spendCount(missing)} returned no final cost. Missing costs are not treated as free.`:'New model requests will be attributed to the signed-in user using the cost returned by LiteLLM.'}</p></div>
    ${admin?`<section class="card spend-section"><div class="section-header"><h2>LLM spend by user</h2><label class="spend-user-filter"><span class="sr-only">Filter user</span><select id="spend-user"><option value="">All users</option>${data.users.map(u=>`<option value="${esc(u.id)}" ${spendState.user===u.id?'selected':''}>${esc(u.email||u.name)}</option>`).join('')}</select></label></div><div class="spend-table-wrap"><table class="spend-table"><thead><tr><th>User</th><th>Sessions</th><th>Requests</th><th>Tokens</th><th>Spend</th></tr></thead><tbody>${users.map(u=>`<tr><td><strong>${esc(u.email||u.name)}</strong><small>${u.kind==='google'?'Google SSO':u.kind==='slack'?esc(slackIdentityStatus(u)):u.kind==='unattributed'?'Earlier or unmatched gateway usage':'Shared sign-in'}</small></td><td>${spendCount(u.sessions)}</td><td>${spendCount(u.requests)}</td><td>${spendCount(u.total_tokens)}</td><td><strong>${dollars(u.spend)}</strong>${(u.pending_costs||u.missing_costs)?`<small>${spendCount(u.pending_costs)} pending · ${spendCount(u.missing_costs)} missing</small>`:''}</td></tr>`).join('')||'<tr><td colspan="5" class="subtext">No recorded model usage in this period.</td></tr>'}</tbody><tfoot><tr><th>${spendState.user?'Selected user':'All users'}</th><td colspan="3"></td><th>${dollars(spendState.user?(users[0]?.spend||0):data.total.spend)}</th></tr></tfoot></table></div><p class="subtext spend-note">Each response is attributed to the person who sent that message. Requests recorded without a user identity stay under “Unattributed / earlier usage.”</p></section>`:''}
    <section class="card spend-section"><h2>${admin?'Sessions':'Your sessions'}${spendState.user?' for selected user':''}</h2><div class="spend-table-wrap"><table class="spend-table"><thead><tr><th>Session</th>${admin?'<th>User</th>':''}<th>Requests</th><th>Spend</th></tr></thead><tbody>${sessions.map(s=>`<tr><td><a href="#run=${esc(s.run_id)}">${esc(s.title)}</a></td>${admin?`<td>${esc(s.user_name)}</td>`:''}<td>${spendCount(s.requests)}</td><td>${dollars(s.spend)}${s.missing_costs?'<small>Cost missing</small>':s.pending_costs?'<small>Cost pending</small>':''}</td></tr>`).join('')||`<tr><td colspan="${admin?4:3}" class="subtext">${admin?'Attributed sessions will appear after a model request.':'No model requests attributed to you in this period.'}</td></tr>`}</tbody></table></div></section>
    <section class="card spend-section"><h2>${admin?'Models · all users':'Your models'}</h2><div class="spend-models">${data.models.map(m=>`<div><span>${esc(modelName(m.model))}</span><strong>${dollars(m.spend)}</strong><small>${spendCount(m.requests)} requests</small></div>`).join('')||'<p class="subtext">No usage yet.</p>'}</div></section>
    <details class="card spend-section"><summary>Request costs · latest 500 in this period</summary><div class="spend-table-wrap"><table class="spend-table"><thead><tr><th>Time</th><th>Model</th><th>Cache read tokens</th><th>Cache write tokens</th><th>Cost source</th><th>Exact USD</th></tr></thead><tbody>${data.request_details.filter(r=>!spendState.user||r.user_id===spendState.user).map(r=>`<tr><td><a href="#run=${esc(r.run_id)}" title="${esc(r.id)}">${esc(new Date(r.created_at).toLocaleString())}</a></td><td>${esc(modelName(r.model))}</td><td>${r.cache_read_input_tokens==null?'Not reported':spendCount(r.cache_read_input_tokens)}</td><td>${r.cache_creation_input_tokens==null?'Not reported':spendCount(r.cache_creation_input_tokens)}</td><td>${r.cost_source==='response_header'?'Response header':r.cost_source==='response_usage'?'Response usage':r.cost!==null?'Earlier record':'Not returned'}</td><td>${r.cost!==null?'$'+esc(r.cost):r.status==='pending'?'In progress':'Unknown'}</td></tr>`).join('')||'<tr><td colspan="6">No requests in this period.</td></tr>'}</tbody></table></div></details>
    ${admin?renderSlackIdentities(slack,google,identityStatus):''}
    <p class="subtext spend-note">LLM costs are saved from Moyai’s inference responses across gateway key rotations. ${admin?'Infrastructure uses the provider reports and monthly bills above.':'Each response is attributed to the person who sent that message. Your view excludes other users and infrastructure costs.'} Calls made with separate credential-proxy keys are not included. Usage before tracking began or outside Moyai is not included. An interrupted or failed request may have no returned cost, so this is not a full audit of the key’s lifetime spend. ${data.tracked_since?'Per-user tracking began '+esc(new Date(data.tracked_since).toLocaleString())+'.':''}</p>`;
  if(admin)bindInfrastructure(data);
  if(admin&&data.infrastructure.pending)spendState.timer=setTimeout(()=>{if(current()&&!$('#infra-bill-editor')?.open)renderSpend().catch(showError);},5000);
  bindSpendFilters();
  if(!admin)return;
  $('#spend-user').onchange=()=>{spendState.user=$('#spend-user').value;renderSpend().catch(showError);};
  $('#refresh-identities').onclick=async()=>{const button=$('#refresh-identities');button.disabled=true;try{await api('/api/admin/identities/refresh',{method:'POST'});button.textContent='Profiles queued';toast('Profile refresh queued. Use Refresh above to see updated matches.');}catch(error){button.disabled=false;showError(error);}};
  document.querySelectorAll('.spend-link-form').forEach(form=>form.onsubmit=async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;try{await api('/api/admin/spend/link-slack',{method:'POST',body:JSON.stringify({slack_user_id:form.dataset.slack,google_user_id:form.elements.google.value})});await renderSpend();toast('Slack usage linked to Google account.');}catch(error){button.disabled=false;showError(error);}});
}
