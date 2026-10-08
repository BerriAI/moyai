const adoptionState={start:'',end:'',request:0};
function adoptionChart(daily){
  return analyticsChart(daily,[{key:'requests',label:'Human requests'}],{title:'Daily human requests and seven-day moving average',line:{key:'seven_day_average'}});
}
function adoptionDashboard(data){
  const w=data.weekly;
  const trend=w.percent_change===null?(w.requests?'No prior-week baseline':'No requests in either week'):`${w.percent_change>0?'+':''}${w.percent_change}% vs previous week`;
  return `<div class="analytics-page"><div class="page-heading"><div><h1>Usage analytics</h1><p class="subtext">Understand how your team uses Moyai over time.</p></div><div class="analytics-actions">${analyticsRange('adoption',data.start,data.end)}<button id="adoption-refresh">Refresh</button></div></div>
    <div class="analytics-toolbar"><div class="analytics-tabs"><a href="#spend">Spend & usage</a><span aria-current="page">Human activity</span></div><button id="adoption-export" class="analytics-export">Export CSV</button></div>
    <section class="analytics-section"><h2>Human requests</h2>${analyticsMetrics([['Requests in period',spendCount(data.total_requests)],['Last 7 complete days',spendCount(w.requests),trend],['Previous week',spendCount(w.previous_requests),w.previous_start+' – '+w.previous_end]])}<div class="analytics-chart-heading"><h3>Requests over time</h3><span>Daily · UTC</span></div>${analyticsLegend([{label:'Human requests'},{label:'7-day average',line:true}])}${data.total_requests?'':'<p class="subtext">No recorded human requests in this period.</p>'}${adoptionChart(data.daily)}<p class="subtext analytics-footnote">Weekly comparison: ${esc(w.start)} – ${esc(w.end)}. Change: ${w.delta>0?'+':''}${spendCount(w.delta)}. Comparisons exclude today.</p></section>
    <section class="analytics-section"><h2>Active teammates</h2>${analyticsMetrics([['Active in period',spendCount(data.active_users),'Distinct identified requesters']])}<div class="analytics-chart-heading"><h3>Active teammates over time</h3><span>Daily · UTC</span></div>${analyticsChart(data.daily,[{key:'active_users',label:'Active teammates'}],{title:'Active teammates over time',area:true})}<p class="subtext analytics-footnote">Today is partial. Linked Slack and Google accounts count as one teammate.</p></section>
    <details class="analytics-breakdown" id="adoption-breakdown"><summary>Daily breakdown</summary><div class="spend-table-wrap" role="region" aria-label="Daily adoption breakdown" tabindex="0"><table class="spend-table"><thead><tr><th>Date (UTC)</th><th>Requests</th><th>7-day average</th><th>Active teammates</th></tr></thead><tbody>${[...data.daily].reverse().map(d=>`<tr><td>${esc(d.date)}${d.partial?' · partial':''}</td><td>${spendCount(d.requests)}</td><td>${d.seven_day_average}</td><td>${spendCount(d.active_users)}</td></tr>`).join('')}</tbody></table></div></details>
    <details class="analytics-methodology" id="adoption-methodology"><summary>What counts as a request?</summary><p class="subtext adoption-definition">One request = one saved human chat submission, including initial prompts, follow-ups, and steering messages from Slack or the web. Submission retries count once. Includes queued, cancelled, failed, and subsequently deleted submissions; excludes demo sessions, delegated agents, and automation launch prompts. Human follow-ups to automations count. LLM calls and tool calls do not count.</p><p class="subtext adoption-definition">Uses retained message history; days without records appear as zero. Earlier non-chat tasks and purged history cannot be reconstructed. Linked Slack and Google accounts count as one teammate; anonymous requests are included in totals, not active teammates. The moving average includes the preceding six days, even outside the selected range. Trends show correlation, not whether a product change caused adoption to change.</p></details></div>`;
}
async function renderAdoption(){
  if(state.role!=='admin'){$('#content').innerHTML='<div class="error-banner">Adoption reports are available to organization administrators.</div>';return;}
  const version=state.pageVersion,request=++adoptionState.request;
  const current=()=>version===state.pageVersion&&request===adoptionState.request;
  const query=new URLSearchParams();if(adoptionState.start)query.set('start',adoptionState.start);if(adoptionState.end)query.set('end',adoptionState.end);
  const preserved=typeof document.querySelector==='function'&&document.querySelector('.analytics-page')?{open:[...document.querySelectorAll('#content details[id][open]')].map(el=>el.id),focus:document.activeElement?.id,scroll:$('#content').scrollTop}:null;
  $('#content').innerHTML='<p class="subtext" role="status">Loading usage…</p>';
  let data;
  try{data=await api('/api/admin/adoption?'+query);}catch(error){
    if(!current())return;
    const today=new Date().toISOString().slice(0,10);
    $('#content').innerHTML=`<div class="analytics-page"><div class="page-heading"><h1>Usage analytics</h1></div><div class="error-banner" role="alert">${esc(error.message)}</div><div class="analytics-actions">${analyticsRange('adoption',adoptionState.start||today,adoptionState.end||today)}<button id="adoption-refresh">Try again</button></div></div>`;
    bindAdoptionFilters();return;
  }
  if(!current())return;
  adoptionState.start=data.start;adoptionState.end=data.end;
  $('#content').innerHTML=adoptionDashboard(data);
  bindAdoptionFilters();
  $('#adoption-export').onclick=()=>downloadAnalyticsCSV(`moyai-usage-${data.start}-${data.end}.csv`,[['Date (UTC)','Human requests','7-day average','Active teammates','Partial day'],...data.daily.map(d=>[d.date,d.requests,d.seven_day_average,d.active_users,d.partial])]);
  if(preserved){for(const id of preserved.open){const el=$('#'+id);if(el)el.open=true;}if(preserved.focus)$('#'+preserved.focus)?.focus({preventScroll:true});$('#content').scrollTop=preserved.scroll;}
}
function bindAdoptionFilters(){
  const apply=range=>{if($('#adoption-range'))$('#adoption-range').open=false;$('#adoption-range-toggle')?.focus?.();Object.assign(adoptionState,range);renderAdoption().catch(showError);};
  $('#adoption-filter-form').onsubmit=e=>{e.preventDefault();apply({start:e.currentTarget.elements.start.value,end:e.currentTarget.elements.end.value});};
  bindAnalyticsPreset('adoption',apply);
  $('#adoption-refresh').onclick=()=>renderAdoption().catch(showError);
}
