/* Organization analytics; called only after /api/spend confirms organization scope. */
const spendAnalyticsState={tab:'overall',model:'',sort:'spend',direction:-1,request:0};
const spendIdentityPanels=new WeakMap();
function isSpendPRTab(tab){return tab==='prs'||tab==='leaderboard';}
function spendDailyBreakdown(data){
  return `<details class="analytics-breakdown" id="spend-daily-breakdown"><summary>Daily breakdown</summary><div class="spend-table-wrap" role="region" aria-label="Daily usage breakdown" tabindex="0"><table class="spend-table"><thead><tr><th>Date (UTC)</th><th>Active sessions</th><th>Active users</th><th>LLM requests</th><th>Tokens</th><th>Unpriced requests</th><th>LLM spend</th></tr></thead><tbody>${(data.daily||[]).map(d=>`<tr><td>${esc(d.date)}</td><td>${spendCount(d.sessions)}</td><td>${spendCount(d.active_users)}</td><td>${spendCount(d.requests)}</td><td>${spendCount(d.total_tokens)}</td><td>${spendCount(d.pending_costs+d.missing_costs)}</td><td>${dollars(d.spend)}</td></tr>`).join('')}</tbody></table></div></details>`;
}
function spendSessions(data){
  const sessions=data.sessions.filter(s=>!spendState.user||s.user_id===spendState.user);
  return `<details class="analytics-breakdown" id="spend-sessions"><summary>Sessions${spendState.user?' for selected user':''} (${sessions.length})</summary><div class="spend-table-wrap" role="region" aria-label="Session spend" tabindex="0"><table class="spend-table"><thead><tr><th>Session</th><th>User</th><th>LLM requests</th><th>LLM spend</th></tr></thead><tbody>${sessions.map(s=>`<tr><td><a href="#run=${esc(s.run_id)}">${esc(s.title)}</a></td><td>${esc(s.user_name)}</td><td>${spendCount(s.requests)}</td><td>${dollars(s.spend)}${s.missing_costs?'<small>Cost missing</small>':s.pending_costs?'<small>Cost pending</small>':''}</td></tr>`).join('')||'<tr><td colspan="4">No sessions in this period.</td></tr>'}</tbody></table></div></details>`;
}
function spendRequests(data){
  return `<details class="analytics-breakdown" id="spend-request-costs"><summary>Request costs · ${spendAnalyticsState.model?'selected model from the latest 500':'latest 500 in this period'}</summary><div class="spend-table-wrap" role="region" aria-label="Exact request costs" tabindex="0"><table class="spend-table"><thead><tr><th>Time</th><th>Model</th><th>Cache read tokens</th><th>Cache write tokens</th><th>Cost source</th><th>Exact USD</th></tr></thead><tbody>${data.request_details.filter(r=>!spendAnalyticsState.model||r.model===spendAnalyticsState.model).map(r=>`<tr><td><a href="#run=${esc(r.run_id)}" title="${esc(r.id)}">${esc(new Date(r.created_at).toLocaleString())}</a></td><td>${esc(modelName(r.model))}</td><td>${r.cache_read_input_tokens==null?'Not reported':spendCount(r.cache_read_input_tokens)}</td><td>${r.cache_creation_input_tokens==null?'Not reported':spendCount(r.cache_creation_input_tokens)}</td><td>${spendCostSource(r)}</td><td>${spendCostValue(r)}</td></tr>`).join('')||'<tr><td colspan="6">No requests in this period.</td></tr>'}</tbody></table></div></details>`;
}
function spendCostNotice(data){
  const pending=data.total.pending_costs||0,missing=data.total.missing_costs||0;
  return `<p class="analytics-status ${pending||missing?'analytics-status-warning':''}" role="status">${pending||missing?`${spendCount(pending)} costs pending · ${spendCount(missing)} missing final costs. Missing costs are not treated as free.`:data.total.requests?'All tracked LLM requests priced.':'No recorded model usage in this period. New requests will appear here.'}</p>`;
}
function spendOverview(data){
  const daily=data.daily||[],total=data.total;
  return `${renderCostSummary(data)}<section class="analytics-section"><h2>Sessions</h2>${analyticsMetrics([['Active sessions',spendCount(total.sessions),'Sessions with model usage'],['LLM cost per session',total.sessions?summaryDollars(Number(total.spend)/total.sessions):'—','Recorded costs only']])}<div class="analytics-chart-heading"><h3>Sessions over time</h3><span>Daily · UTC</span></div>${analyticsLegend([{label:'Active sessions'}])}${analyticsChart(daily,[{key:'sessions',label:'Active sessions'}],{title:'Active sessions over time'})}<p class="subtext analytics-footnote">A session counts on each day it makes a model request. The period total counts each session once.</p></section>
  <section class="analytics-section"><h2>Model usage</h2>${analyticsMetrics([['LLM requests',spendCount(total.requests)],['Tokens',spendCount(total.total_tokens)],['LLM cost per request',total.requests?summaryDollars(Number(total.spend)/total.requests):'—','Recorded costs only']])}<div class="analytics-chart-heading"><h3>LLM requests over time</h3><span>Daily · UTC</span></div>${analyticsChart(daily,[{key:'requests',label:'LLM requests'}],{title:'LLM requests over time',area:true})}${spendCostNotice(data)}</section>${spendDailyBreakdown(data)}`;
}
function spendUserRows(data){
  const {sort,direction}=spendAnalyticsState;
  return data.users.filter(u=>!spendState.user||u.id===spendState.user).sort((a,b)=>direction*(sort==='name'?(a.email||a.name).localeCompare(b.email||b.name):Number(a[sort]||0)-Number(b[sort]||0)));
}
function spendUsersPanel(data){
  const users=spendUserRows(data),active=data.users.filter(u=>u.requests>0&&u.kind!=='unattributed').length;
  const heading=(key,label)=>`<th aria-sort="${spendAnalyticsState.sort===key?(spendAnalyticsState.direction===1?'ascending':'descending'):'none'}"><button class="analytics-sort" data-spend-sort="${key}">${label}${spendAnalyticsState.sort===key?(spendAnalyticsState.direction===1?' ↑':' ↓'):''}</button></th>`;
  return `<section class="analytics-section"><div class="section-header"><h2>LLM spend by user</h2><label class="spend-user-filter"><span class="sr-only">Filter user</span><select id="spend-user"><option value="">All users</option>${data.users.map(u=>`<option value="${esc(u.id)}" ${spendState.user===u.id?'selected':''}>${esc(u.email||u.name)}</option>`).join('')}</select></label></div><p class="subtext analytics-description">Across the team: ${spendCount(active)} users with model usage · ${summaryDollars(data.total.spend)} recorded LLM spend. The user filter applies to this table and sessions; activity above stays team-wide.</p><div class="analytics-table-frame spend-table-wrap" role="region" aria-label="LLM spend by user" tabindex="0"><table class="spend-table spend-users-table"><thead><tr>${heading('name','User')}${heading('spend','Total cost')}${heading('sessions','Sessions')}${heading('requests','LLM requests')}${heading('total_tokens','Tokens')}<th>$ / session</th><th>Share of LLM spend</th></tr></thead><tbody>${users.map(u=>{const share=data.total.spend>0?Number(u.spend)/Number(data.total.spend)*100:0;return `<tr><td><strong>${esc(u.email||u.name)}</strong><small>${esc(u.name)}${u.kind==='unattributed'?' · unmatched usage':''}</small></td><td>${summaryDollars(u.spend)}${u.pending_costs||u.missing_costs?`<small>${spendCount(u.pending_costs)} pending · ${spendCount(u.missing_costs)} missing</small>`:''}</td><td>${spendCount(u.sessions)}</td><td>${spendCount(u.requests)}</td><td>${spendCount(u.total_tokens)}</td><td>${u.sessions?summaryDollars(Number(u.spend)/u.sessions):'—'}</td><td><div class="analytics-share"><span aria-hidden="true"><i style="width:${share}%"></i></span><small>${share.toFixed(1)}%</small></div></td></tr>`;}).join('')||'<tr><td colspan="7">No recorded model usage in this period.</td></tr>'}</tbody><tfoot><tr><th>${spendState.user?'Selected user':'All users'}</th><td>${summaryDollars(users.reduce((sum,u)=>sum+Number(u.spend),0))}</td><td colspan="5"></td></tr></tfoot></table></div>${spendState.user?'<button class="quiet" id="spend-clear-user">Clear user filter</button>':''}<p class="subtext analytics-footnote">Each response is attributed to its sender. Linked Slack and Google accounts count as one user.</p></section>${spendSessions(data)}`;
}
function spendHistoryData(data){
  const selected=spendAnalyticsState.model;
  const models=data.models.filter(m=>!selected||m.model===selected).sort((a,b)=>Number(b.spend)-Number(a.spend));
  const series=models.slice(0,4).map((m,i)=>({key:'model'+i,label:modelName(m.model),model:m.model}));
  if(models.length>4)series.push({key:'other',label:'Other models'});
  const rows=(data.daily||[]).map(day=>{const row={date:day.date,pending_costs:0,missing_costs:0,requests:0,spend:0};for(const m of day.models){if(selected&&selected!==m.model)continue;const key=series.find(s=>s.model===m.model)?.key||'other';row[key]=(row[key]||0)+Number(m.spend);row.spend+=Number(m.spend);row.pending_costs+=m.pending_costs||0;row.missing_costs+=m.missing_costs||0;row.requests+=m.requests;}return row;});
  return {series,rows,models,total:models.reduce((sum,m)=>sum+Number(m.spend),0)};
}
function spendHistoryPanel(data){
  const {series,rows,models,total}=spendHistoryData(data);
  const filteredTotal=rows.reduce((sum,r)=>({requests:sum.requests+r.requests,pending_costs:sum.pending_costs+r.pending_costs,missing_costs:sum.missing_costs+r.missing_costs}),{requests:0,pending_costs:0,missing_costs:0});
  return `<section class="analytics-section"><div class="section-header"><h2>Usage history</h2><label><span class="sr-only">Filter model</span><select id="spend-model"><option value="">All models</option>${data.models.map(m=>`<option value="${esc(m.model)}" ${spendAnalyticsState.model===m.model?'selected':''}>${esc(modelName(m.model))}</option>`).join('')}</select></label></div><div class="analytics-history-card"><span class="subtext">Recorded LLM spend</span><strong class="analytics-total">${summaryDollars(total)}</strong><div class="analytics-composition" aria-hidden="true">${series.map((s,i)=>`<i class="analytics-color-${i}" style="width:${total?rows.reduce((sum,r)=>sum+(r[s.key]||0),0)/total*100:0}%"></i>`).join('')}</div>${analyticsLegend(series,true)}${analyticsChart(rows,series,{title:'Daily LLM spend and cumulative cost',money:true,cumulative:true})}<p class="subtext analytics-footnote">Daily cost on the left · cumulative cost on the right · USD</p></div>${spendCostNotice({total:filteredTotal})}<p class="subtext analytics-footnote">Infrastructure is reported separately in the Infrastructure tab.</p></section>
  <section class="analytics-section"><h2>Models</h2><div class="analytics-table-frame spend-table-wrap" role="region" aria-label="Model usage" tabindex="0"><table class="spend-table"><thead><tr><th>Model</th><th>LLM requests</th><th>Tokens</th><th>LLM spend</th></tr></thead><tbody>${models.map(m=>`<tr><td>${esc(modelName(m.model))}</td><td>${spendCount(m.requests)}</td><td>${spendCount(m.total_tokens)}</td><td>${summaryDollars(m.spend)}</td></tr>`).join('')||'<tr><td colspan="4">No usage yet.</td></tr>'}</tbody></table></div></section><details class="analytics-breakdown" id="spend-history-breakdown"><summary>Daily cost breakdown</summary><div class="spend-table-wrap" role="region" aria-label="Daily cost breakdown" tabindex="0"><table class="spend-table"><thead><tr><th>Date (UTC)</th><th>LLM requests</th><th>Unpriced requests</th><th>Recorded LLM spend</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.date)}</td><td>${spendCount(r.requests)}</td><td>${spendCount(r.pending_costs+r.missing_costs)}</td><td>${dollars(r.spend)}</td></tr>`).join('')}</tbody></table></div></details>${spendRequests(data)}`;
}
function spendExport(data,activity=null){
  const tab=spendAnalyticsState.tab;
  if(isSpendPRTab(tab))return spendPRExport(tab);
  if(tab==='users'){
    const users=[['User','Name','Sessions','LLM requests','Tokens','LLM spend USD','Pending costs','Missing costs'],...spendUserRows(data).map(u=>[u.email||u.id,u.name,u.sessions,u.requests,u.total_tokens,u.spend,u.pending_costs,u.missing_costs])];
    return activity?[...users,[],['Team activity (all users)'],...adoptionExport(activity)]:users;
  }
  if(tab==='history'){const {rows,series}=spendHistoryData(data);return [['Date (UTC)',...series.map(s=>s.label+' USD'),'Recorded LLM spend USD','Pending costs','Missing costs'],...rows.map(r=>[r.date,...series.map(s=>r[s.key]||0),r.spend,r.pending_costs,r.missing_costs])];}
  if(tab==='infrastructure')return [['Provider','Cost USD','Estimated USD','Covered days','Missing days'],...data.infrastructure.providers.map(p=>[p.name,p.spend,p.estimated,p.covered_days,p.missing_days])];
  return [['Date (UTC)','Active sessions','Active users','LLM requests','Tokens','LLM spend USD','Pending costs','Missing costs'],...(data.daily||[]).map(d=>[d.date,d.sessions,d.active_users,d.requests,d.total_tokens,d.spend,d.pending_costs,d.missing_costs])];
}
async function renderAdminSpend(data,identityStatus,activity=null,refresh=false,recent=false,background=false){
  clearTimeout(spendPRState.timer);
  prepareSpendPRReport(data);
  const version=state.pageVersion,spendVersion=spendState.version,request=++spendAnalyticsState.request;
  const current=()=>version===state.pageVersion&&spendVersion===spendState.version&&request===spendAnalyticsState.request;
  if(!data.users.some(u=>u.id===spendState.user))spendState.user='';
  if(!data.models.some(m=>m.model===spendAnalyticsState.model))spendAnalyticsState.model='';
  const tabs=[['overall','Overall'],['users','Users'],['history','Usage history'],['prs','Pull requests'],['leaderboard','Leaderboard'],['infrastructure','Infrastructure']],tab=spendAnalyticsState.tab;
  const identityContext=[spendContext(data.start,data.end),state.userId,data.scope].join('/');
  const previousIdentities=tab==='infrastructure'?$('#spend-identities'):null;
  const previousIdentityPanel=previousIdentities&&spendIdentityPanels.get(previousIdentities);
  const retainedIdentities=previousIdentityPanel?.context===identityContext&&previousIdentityPanel.html?previousIdentities:null;
  const identityPanel=()=>identityStatus?renderSlackIdentities(data.identities.filter(u=>u.kind==='slack'),data.identities.filter(u=>['google','cloudflare'].includes(u.kind)),identityStatus):'<p class="subtext" role="status">Loading account links…</p>';
  const panel=isSpendPRTab(tab)?spendPRPanel(tab):tab==='users'?`<div id="spend-activity"><p class="subtext" role="status">Loading team activity…</p></div>${spendUsersPanel(data)}<div id="spend-activity-details"></div>`:tab==='history'?spendHistoryPanel(data):tab==='infrastructure'?renderCostSummary(data)+renderInfrastructure(data)+`<div id="spend-identities">${retainedIdentities?'':identityPanel()}</div><div id="spend-identities-notice"></div>`:spendOverview(data);
  MoyaiUI.render($('#content'), `<div class="analytics-page"><div class="page-heading"><div><h1>Spend & usage</h1><p class="subtext">Your team's costs and activity, at a glance.</p></div><div class="analytics-actions">${analyticsRange('spend',data.start,data.end)}<button id="sync-spend">Refresh</button></div></div><div class="analytics-toolbar"><div class="analytics-tabs" role="group" aria-label="Spend and usage report view">${tabs.map(([key,label])=>`<button id="spend-tab-${key}" data-spend-tab="${key}" aria-pressed="${tab===key}">${label}</button>`).join('')}</div><button class="analytics-export" id="spend-export" ${tab==='users'||(isSpendPRTab(tab)&&(!spendPRState.data||spendPRState.promise||spendPRState.error))?'disabled':''}>Export CSV</button></div><div id="spend-refresh-status" role="status"></div><div id="spend-panel">${panel}</div><details class="analytics-methodology" id="spend-methodology"><summary>About this data</summary><p>LLM costs are saved from Moyai’s inference responses across gateway key rotations. Calls using separate credential-proxy keys and usage outside Moyai are excluded. Failed or interrupted requests may have no returned cost. Infrastructure uses provider reports and monthly bills; estimates and missing coverage are marked.</p>${data.tracked_since?`<p>Per-user tracking began ${esc(new Date(data.tracked_since).toLocaleString())}.</p>`:''}</details></div>`,{preserve:retainedIdentities?[retainedIdentities]:[]});
  // Reattach the independently mounted account controls before the browser paints.
  if(retainedIdentities)$('#spend-identities').replaceWith(retainedIdentities);
  bindSpendFilters();
  const redraw=focus=>{const rendered=renderAdminSpend(data,identityStatus,activity,false,true);if(focus)$('#'+focus)?.focus();return rendered;};
  document.querySelectorAll('[data-spend-tab]').forEach(button=>button.onclick=()=>{spendAnalyticsState.tab=button.dataset.spendTab;return redraw(button.id).catch(showError);});
  $('#spend-export').onclick=()=>{if(!$('#spend-export').disabled)downloadAnalyticsCSV(`moyai-${tab}-${data.start}-${data.end}.csv`,spendExport(data));};
  if(isSpendPRTab(tab))loadSpendPRReport(data,tab,request,refresh);
  if(tab==='users'){
    $('#spend-user').onchange=()=>{spendState.user=$('#spend-user').value;redraw('spend-user').catch(showError);};
    if($('#spend-clear-user'))$('#spend-clear-user').onclick=()=>{spendState.user='';redraw('spend-user').catch(showError);};
    document.querySelectorAll('[data-spend-sort]').forEach(button=>button.onclick=()=>{const key=button.dataset.spendSort;spendAnalyticsState.direction=spendAnalyticsState.sort===key?-spendAnalyticsState.direction:-1;spendAnalyticsState.sort=key;redraw().catch(showError);document.querySelector(`[data-spend-sort="${key}"]`)?.focus();});
  }
  if(tab==='history')$('#spend-model').onchange=()=>{spendAnalyticsState.model=$('#spend-model').value;redraw('spend-model').catch(showError);};
  if(tab==='infrastructure'){
    bindInfrastructure(data);
    const host=$('#spend-identities'),panel=retainedIdentities?spendIdentityPanels.get(host):{context:identityContext,html:null};
    spendIdentityPanels.set(host,panel);
    try{
      if(!identityStatus)identityStatus=await api('/api/admin/identities/status',{recent});
      if(!current())return false;
      const html=identityPanel();
      if(panel.html!==html||refresh&&!background){
        if(retainedIdentities&&background&&(document.hidden||settingsInteractionActive()))return true;
        const restore=retainedIdentities?preserveSpendView():()=>{};
        MoyaiUI.render(host,html);bindSpendIdentities();restore();panel.html=html;
      }
    }catch(error){
      if(!current())return false;
      // Keep usable controls during a transient outage; access failures remove them.
      if(!panel.html||error.status===401||error.status===403){MoyaiUI.render(host,'');panel.html=null;}
      MoyaiUI.render($('#spend-identities-notice'),`<p role="alert">Could not load account links. ${esc(error.message)}</p><button id="spend-identities-retry">Retry</button>`);
      $('#spend-identities-retry').onclick=()=>renderAdminSpend(data,null,activity).catch(showError);
    }
  }
  if(tab==='users'){
    const query=new URLSearchParams({start:data.start,end:data.end});
    try{
      if(!activity)activity=await api('/api/admin/adoption?'+query,{recent});
      if(!current())return false;
      MoyaiUI.render($('#spend-activity'), adoptionDashboard(activity));
      MoyaiUI.render($('#spend-activity-details'), adoptionDetails(activity));
      $('#spend-export').disabled=false;
      $('#spend-export').onclick=()=>downloadAnalyticsCSV(`moyai-users-${data.start}-${data.end}.csv`,spendExport(data,activity));
    }catch(error){
      if(!current())return false;
      MoyaiUI.render($('#spend-activity'), `<div class="error-banner" role="alert">Unable to load team activity. ${esc(error.message)} Spend by user is still available below.</div><button id="activity-retry">Try again</button>`);
      $('#spend-export').disabled=false;
      $('#spend-export').textContent='Export spend CSV';
      $('#activity-retry').onclick=()=>renderAdminSpend(data,identityStatus,null).catch(showError);
    }
  }
  return true;
}
function bindSpendIdentities(){
  $('#refresh-identities').onclick=async()=>{const button=$('#refresh-identities');button.disabled=true;try{await api('/api/admin/identities/refresh',{method:'POST'});button.textContent='Profiles queued';toast('Profile refresh queued. Use Refresh above to see updated matches.');}catch(error){button.disabled=false;showError(error);}};
  document.querySelectorAll('.spend-link-form').forEach(form=>form.onsubmit=async e=>{e.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;try{await api('/api/admin/spend/link-slack',{method:'POST',body:JSON.stringify({slack_user_id:form.dataset.slack,google_user_id:form.elements.google.value})});await renderSpend();toast('Slack usage linked to Google account.');}catch(error){button.disabled=false;showError(error);}});
}
