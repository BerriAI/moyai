/* PR reports share the authenticated organization Spend scope and date controls. */
const spendPRState={key:'',data:null,promise:null,error:null,timer:null,search:'',status:'',contributor:''};
function spendPRKey(data){return [state.pageVersion,data.scope,data.start,data.end].join('/');}
function prepareSpendPRReport(data){
  const key=spendPRKey(data);
  if(spendPRState.key!==key){spendPRState.key=key;spendPRState.data=null;spendPRState.promise=null;spendPRState.error=null;}
}
function spendPRURL(row){
  try{
    const url=new URL(row.url),match=url.pathname.match(/^\/([A-Za-z0-9-]+)\/([A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]*)$/);
    if(url.protocol!=='https:'||url.hostname!=='github.com'||url.port||url.username||url.password||url.search||url.hash||!match||['.','..'].includes(match[2])||Number(match[3])!==Number(row.number))return '';
    return url.origin+url.pathname;
  }catch{return '';}
}
function spendPRStatus(row){return row.state==='open'&&row.draft?'draft':['open','closed','merged'].includes(row.state)?row.state:'unknown';}
function spendPRCost(row){
  const amount=row.spend==null?'Unavailable':summaryDollars(row.spend),pending=row.pending_costs||0,missing=row.missing_costs||0;
  return `${amount}${pending||missing?`<small class="analytics-status-warning">${spendCount(pending)} pending · ${spendCount(missing)} missing</small>`:''}`;
}
function spendPRRows(data){
  const search=spendPRState.search.trim().toLowerCase();
  return (data.pull_requests||[]).filter(row=>(!spendPRState.status||spendPRStatus(row)===spendPRState.status)&&(!search||[row.title,row.url,row.number,row.user_name,row.user_email].join(' ').toLowerCase().includes(search)));
}
function spendPRTable(rows,label){
  return `<div class="analytics-table-frame spend-table-wrap" role="region" aria-label="${esc(label)}" tabindex="0"><table class="spend-table analytics-pr-table"><thead><tr><th>Pull request</th><th>Contributor</th><th>Status</th><th>Linked sessions</th><th>LLM requests</th><th>Linked session LLM spend<small>All time · USD</small></th></tr></thead><tbody>${rows.map(row=>{
    const url=spendPRURL(row),status=spendPRStatus(row),title=row.title||'Pull request #'+row.number,repo=url?new URL(url).pathname.split('/').slice(1,3).join('/'):'Repository unavailable';
    return `<tr><td>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer"><strong>${esc(title)}</strong></a>`:`<strong>${esc(title)}</strong>`}<small>${esc(repo)} #${esc(row.number)}</small><small>${row.created_at?'Created '+esc(row.created_at.slice(0,10))+' · ':''}Tracked ${esc((row.tracked_at||'').slice(0,10)||'Unknown')}${row.merged_at?' · Merged '+esc(row.merged_at.slice(0,10)):''}</small></td><td>${esc(row.user_name||'Unattributed')}${row.user_email?`<small>${esc(row.user_email)}</small>`:''}</td><td><span class="badge ${status==='merged'?'complete':status==='unknown'?'pr-status-unknown':'pr-status-neutral'}">${esc(status[0].toUpperCase()+status.slice(1))}</span>${row.stale?'<small class="analytics-status-warning">Status may be outdated</small>':''}</td><td>${(row.sessions||[]).map(session=>`<small>${session.deleted?`${esc(session.title||session.id)} · Deleted`:`<a href="#run=${esc(encodeURIComponent(session.id))}">${esc(session.title||session.id)}</a>`}</small>`).join('')||'None'}</td><td>${spendCount(row.requests)}</td><td>${spendPRCost(row)}</td></tr>`;
  }).join('')||'<tr><td colspan="6">No pull requests match this view.</td></tr>'}</tbody></table></div>`;
}
function spendPRNotice(data){
  const unknown=data.unknown_status||0,stale=data.stale_status||0,unknownCreated=data.unknown_created_at||0;
  return `<p class="analytics-status ${unknown||stale||unknownCreated?'analytics-status-warning':''}" role="status">${data.pending_refresh?'Checking GitHub status… ':''}${unknown||stale||unknownCreated?`Across all tracked PRs: ${spendCount(unknown)} with unknown status or merge date · ${spendCount(stale)} with status that may be outdated${unknownCreated?' · '+spendCount(unknownCreated)+' with unknown creation date':''}. Creation and merge counts use verified dates.`:data.pending_refresh?'The report updates as checks finish.':'GitHub status checks complete.'}</p>`;
}
function spendPRMethodology(){
  return '<details class="analytics-methodology" id="spend-pr-methodology"><summary>About PR attribution and costs</summary><p>Credit goes to the person who requested the original PR in Moyai. Linked Slack and Google accounts count as one contributor.</p><p>Linked session LLM spend includes all recorded usage in each linked session and its direct child sessions, including follow-up work. Sessions can appear under several PRs, so PR costs overlap and must not be added together. Contributor spend counts each linked session once per contributor and can also overlap between contributors. Pending and missing costs are not treated as free.</p></details>';
}
function spendPRList(data){
  const rows=spendPRRows(data),filtered=spendPRState.search||spendPRState.status;
  return `<section class="analytics-section"><div class="section-header"><div><h2>Pull requests</h2><p class="subtext">First tracked in the selected dates · ${spendCount((data.pull_requests||[]).length)} pull requests</p></div></div><div class="analytics-pr-filters"><label>Search pull requests<input id="spend-pr-search" type="search" placeholder="Title, repository, or contributor" value="${esc(spendPRState.search)}"></label><label>Status<select id="spend-pr-status"><option value="">All statuses</option>${['merged','open','draft','closed','unknown'].map(status=>`<option value="${status}" ${spendPRState.status===status?'selected':''}>${status[0].toUpperCase()+status.slice(1)}</option>`).join('')}</select></label>${filtered?'<button id="spend-pr-clear" class="quiet">Clear filters</button>':''}</div>${rows.length?spendPRTable(rows,'Pull requests first tracked in selected dates'):`<p class="analytics-status" role="status">${filtered?'No pull requests match these filters. Clear filters to see all tracked PRs in this period.':'No pull requests were first tracked in this period. Choose another date range or publish a PR from a Moyai session.'}</p>`}${spendPRNotice(data)}${spendPRMethodology()}</section>`;
}
function spendPRLeaders(data){
  return [...(data.leaderboard||[])].sort((a,b)=>b.merged_prs-a.merged_prs||(a.name||'Unattributed').localeCompare(b.name||'Unattributed'));
}
const spendPRStatuses=[['merged','Merged'],['open','Open'],['draft','Draft'],['closed','Closed'],['unknown','Unknown']];
function spendPRDistribution(row){
  const counts=row.status_counts||{},created=row.created_prs||0;
  if(!created)return '<span class="subtext">No PRs created</span>';
  return `<div class="analytics-pr-distribution" role="group" aria-label="Statuses of PRs created in the selected period"><div class="analytics-pr-status-bar" aria-hidden="true">${spendPRStatuses.map(([status,label])=>`<i class="pr-distribution-${status}" style="width:${Math.min(100,Math.max(0,Number(counts[status]||0)/created*100))}%" title="${label}: ${spendCount(counts[status])}"></i>`).join('')}</div><div class="analytics-pr-status-counts">${spendPRStatuses.filter(([status])=>counts[status]>0).map(([status,label])=>`<span>${label} ${spendCount(counts[status])}</span>`).join('<span aria-hidden="true"> · </span>')}</div></div>`;
}
function spendPRMergedCost(row){
  const partial=row.pending_costs||row.missing_costs;
  return `${row.cost_per_merged_pr==null?'—':summaryDollars(row.cost_per_merged_pr)}${partial?`<small class="analytics-status-warning">Partial · ${spendCount(row.pending_costs)} pending · ${spendCount(row.missing_costs)} missing</small>`:row.merged_prs?'':'<small>No merged PRs</small>'}`;
}
function spendPRContributorDetail(data,selected){
  const own=rows=>(rows||[]).filter(row=>(row.user_id||'unattributed')===spendPRState.contributor),created=own(data.created_pull_requests),merged=own(data.merged_pull_requests);
  return `<div class="analytics-pr-detail"><div class="section-header"><h3>Pull requests · ${esc(selected.name||'Unattributed')}</h3><button class="quiet" id="spend-pr-close-contributor">Close details</button></div><h4>Created in period (${spendCount(created.length)})</h4>${created.length?spendPRTable(created,'Pull requests created in the selected period for selected contributor'):'<p class="subtext">No verified PRs created in this period.</p>'}<h4>Merged in period (${spendCount(merged.length)})</h4>${merged.length?spendPRTable(merged,'Pull requests merged in the selected period for selected contributor'):'<p class="subtext">No verified PRs merged in this period.</p>'}</div>`;
}
function spendPRLeaderboard(data){
  const leaders=spendPRLeaders(data),selected=leaders.find(row=>(row.user_id||'unattributed')===spendPRState.contributor);
  let rank=0;
  return `<section class="analytics-section"><h2>PR leaderboard</h2><p class="subtext">Created and merged in the selected dates · UTC · Ranked by merged PRs</p>${analyticsMetrics([['PRs created',spendCount(data.total_created),'Verified GitHub creation dates'],['PRs merged',spendCount(data.total_merged),'Each PR counted once'],['Contributors',spendCount(data.contributors),'People who requested these PRs']])}${leaders.length?`<div class="analytics-pr-status-legend" aria-label="PR status colors">${spendPRStatuses.map(([status,label])=>`<span><i class="pr-distribution-${status}" aria-hidden="true"></i>${label}</span>`).join('')}</div><div class="analytics-table-frame spend-table-wrap" role="region" aria-label="Pull request leaderboard" tabindex="0"><table class="spend-table analytics-pr-leaderboard"><thead><tr><th>Contributor</th><th>PRs created</th><th>PRs by status</th><th>PRs merged</th><th>$ per PR merged</th></tr></thead><tbody>${leaders.map(row=>{const id=row.user_id||'unattributed';if(row.user_id&&row.user_id!=='unattributed')rank++;return `<tr><td><div class="analytics-pr-person"><span class="analytics-pr-rank">${row.user_id&&row.user_id!=='unattributed'?rank:'—'}</span><div><button class="analytics-pr-person-link" data-pr-contributor="${esc(id)}" aria-expanded="${spendPRState.contributor===id}">${esc(row.name||'Unattributed')}</button>${row.email?`<small>${esc(row.email)}</small>`:''}</div></div></td><td><strong>${spendCount(row.created_prs)}</strong></td><td>${spendPRDistribution(row)}</td><td><strong>${spendCount(row.merged_prs)}</strong></td><td>${spendPRMergedCost(row)}</td></tr>`;}).join('')}</tbody></table></div>`:'<p class="analytics-status" role="status">No verified PRs created or merged in this period. Choose another date range or refresh after publishing a PR.</p>'}${spendPRNotice(data)}<p class="subtext analytics-footnote">Status bars show the current statuses of PRs created in this period; merges use the merge date. $ per PR merged = linked session LLM spend ÷ merged PRs. Linked spend covers all time and can overlap between PRs and contributors. Select a contributor for details.</p>${spendPRMethodology()}${selected?spendPRContributorDetail(data,selected):''}</section>`;
}
function spendPRPanel(tab){
  const data=spendPRState.data,error=spendPRState.error;
  return `${error?`<div class="error-banner" role="alert">Could not load pull request analytics. ${esc(error.message)} <button id="spend-pr-retry">Retry</button></div>`:''}${data?(tab==='leaderboard'?spendPRLeaderboard(data):spendPRList(data)):error?'':'<p class="subtext" role="status">Loading pull request analytics…</p>'}`;
}
function bindSpendPRControls(data,tab,request){
  const redraw=focus=>{if(!spendPRCurrent(data,tab,request))return;paintSpendPRPanel(data,tab,request);if(focus)$('#'+focus)?.focus();};
  if(spendPRState.error)$('#spend-pr-retry').onclick=()=>loadSpendPRReport(data,tab,request,true);
  if(!spendPRState.data)return;
  if(tab==='prs'){
    $('#spend-pr-search').oninput=()=>{const input=$('#spend-pr-search'),position=input.selectionStart;spendPRState.search=input.value;redraw('spend-pr-search');$('#spend-pr-search')?.setSelectionRange?.(position,position);};
    $('#spend-pr-status').onchange=()=>{spendPRState.status=$('#spend-pr-status').value;redraw('spend-pr-status');};
    if(spendPRState.search||spendPRState.status)$('#spend-pr-clear').onclick=()=>{spendPRState.search='';spendPRState.status='';redraw('spend-pr-search');};
  }else{
    document.querySelectorAll('[data-pr-contributor]').forEach(button=>button.onclick=()=>{spendPRState.contributor=spendPRState.contributor===button.dataset.prContributor?'':button.dataset.prContributor;redraw();[...document.querySelectorAll('[data-pr-contributor]')].find(next=>next.dataset.prContributor===button.dataset.prContributor)?.focus();});
    if(spendPRState.contributor&&$('#spend-pr-close-contributor'))$('#spend-pr-close-contributor').onclick=()=>{spendPRState.contributor='';redraw();$('#spend-tab-leaderboard')?.focus();};
  }
}
function paintSpendPRPanel(data,tab,request){
  const restore=preserveSpendView(false);
  const html=spendPRPanel(tab);
  if($('#spend-panel').innerHTML!==html)$('#spend-panel').innerHTML=html;
  bindSpendPRControls(data,tab,request);
  restore();
}
function spendPRCurrent(data,tab,request){return state.view==='spend'&&spendPRState.key===spendPRKey(data)&&spendAnalyticsState.tab===tab&&spendAnalyticsState.request===request;}
async function loadSpendPRReport(data,tab,request,refresh=false,background=false){
  if(!spendPRCurrent(data,tab,request))return;
  // The parent may have just replaced the DOM with cached data. Bind before
  // awaiting transport (and before clearing an error whose Retry is visible).
  bindSpendPRControls(data,tab,request);
  // Reuse a same-context request even when the parent refreshes faster than
  // this endpoint responds; otherwise every poll supersedes the last response.
  if(refresh)spendPRState.error=null;
  if(spendPRState.data&&!refresh&&!spendPRState.promise){bindSpendPRControls(data,tab,request);scheduleSpendPRRefresh(data,tab,request);return;}
  $('#spend-export').disabled=true;
  const query=new URLSearchParams({start:data.start,end:data.end});
  const promise=spendPRState.promise||(spendPRState.promise=api('/api/admin/pull-requests?'+query));
  try{
    const report=await promise;
    if(!spendPRCurrent(data,tab,request)||spendPRState.promise!==promise)return;
    if(background&&(document.hidden||typeof settingsInteractionActive==='function'&&settingsInteractionActive())){spendPRState.promise=null;scheduleSpendPRRefresh(data,tab,request);$('#spend-export').disabled=!spendPRState.data||!!spendPRState.error;return;}
    spendPRState.data=report;spendPRState.error=null;
  }catch(error){
    if(!spendPRCurrent(data,tab,request)||spendPRState.promise!==promise)return;
    if(error.status===401||error.status===403)spendPRState.data=null;
    spendPRState.error=error;
  }
  if(!spendPRCurrent(data,tab,request))return;
  spendPRState.promise=null;
  paintSpendPRPanel(data,tab,request);
  $('#spend-export').disabled=!spendPRState.data||!!spendPRState.error;
  // A transient outage must not strand a retained pending-status report.
  scheduleSpendPRRefresh(data,tab,request);
}
function scheduleSpendPRRefresh(data,tab,request){
  clearTimeout(spendPRState.timer);
  if(spendPRState.data?.pending_refresh)spendPRState.timer=setTimeout(()=>{if(!spendPRCurrent(data,tab,request))return;if(document.hidden||typeof settingsInteractionActive==='function'&&document.querySelector('#spend-panel')?.contains(document.activeElement)&&settingsInteractionActive()){scheduleSpendPRRefresh(data,tab,request);return;}loadSpendPRReport(data,tab,request,true,true);},4000);
}
function spendPRExport(tab){
  const data=spendPRState.data;
  if(!data)return [];
  if(tab==='leaderboard')return [['Contributor','Email','PRs created in period',...spendPRStatuses.map(([,label])=>label+' PRs (created in period)'),'PRs merged in period','Cost per merged PR USD (linked spend / merged PRs)','Cost coverage','Linked sessions (merged PRs)','Linked session LLM spend (all time, merged PRs) USD','LLM requests','Pending costs','Missing costs'],...spendPRLeaders(data).map(row=>[row.name||'Unattributed',row.email,row.created_prs,...spendPRStatuses.map(([status])=>row.status_counts?.[status]||0),row.merged_prs,row.cost_per_merged_pr,row.cost_per_merged_pr==null?'No merged PRs':row.pending_costs||row.missing_costs?'Partial':'Recorded costs',row.sessions,row.spend,row.requests,row.pending_costs,row.missing_costs])];
  return [['Pull request','GitHub URL','Repository ID','PR number','Contributor','Email','Status','Status stale','First tracked (UTC)','Merged (UTC)','Linked sessions','Linked session LLM spend (all time, may overlap) USD','LLM requests','Pending costs','Missing costs'],...spendPRRows(data).map(row=>[row.title,spendPRURL(row),row.repository_id,row.number,row.user_name||'Unattributed',row.user_email,spendPRStatus(row),row.stale,row.tracked_at,row.merged_at,(row.sessions||[]).map(s=>s.id+(s.deleted?' (deleted)':'')).join('; '),row.spend,row.requests,row.pending_costs,row.missing_costs])];
}
