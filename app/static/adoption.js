const adoptionState={start:'',end:'',request:0};
function adoptionChart(daily){
  const width=900,height=240,left=44,right=16,top=20,bottom=36;
  const plotWidth=width-left-right,plotHeight=height-top-bottom;
  const max=Math.max(1,...daily.map(d=>Math.max(d.requests,d.seven_day_average)));
  const step=plotWidth/Math.max(1,daily.length),x=i=>left+step*(i+.5),y=v=>top+plotHeight*(1-v/max);
  const grid=[0,.5,1].map(f=>`<line x1="${left}" x2="${width-right}" y1="${y(max*f)}" y2="${y(max*f)}" class="adoption-grid"/><text x="${left-9}" y="${y(max*f)+4}" text-anchor="end">${Number((max*f).toFixed(1))}</text>`).join('');
  const bars=daily.map((d,i)=>`<rect x="${x(i)-step*.32}" y="${y(d.requests)}" width="${step*.64}" height="${plotHeight*d.requests/max}" class="adoption-bar ${d.partial?'partial':''}"><title>${esc(d.date)}: ${d.requests} requests${d.partial?' (partial day)':''}</title></rect>`).join('');
  const line=daily.map((d,i)=>`${x(i)},${y(d.seven_day_average)}`).join(' ');
  const ticks=[...new Set([0,Math.floor((daily.length-1)/2),daily.length-1])].filter(i=>daily[i]).map(i=>`<text x="${x(i)}" y="${height-9}" text-anchor="${i===0?'start':i===daily.length-1?'end':'middle'}">${esc(daily[i].date)}</text>`).join('');
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily human requests and seven-day moving average. Exact values are in the daily breakdown below.">${grid}${bars}<polyline points="${line}" class="adoption-average"/>${ticks}</svg>`;
}
function adoptionDashboard(data){
  const w=data.weekly;
  const trend=w.percent_change===null?(w.requests?'No prior-week baseline':'No requests in either week'):`${w.percent_change>0?'+':''}${w.percent_change}% vs previous week`;
  return `<div class="page-heading"><div><h1>Adoption</h1><p class="subtext">Understand how your team uses Moyai over time.</p></div><span class="badge">UTC</span></div>
    <form id="adoption-filters" class="spend-filters"><label>From<input name="start" type="date" value="${esc(data.start)}" required></label><label>Through<input name="end" type="date" value="${esc(data.end)}" required></label><button type="submit">Apply dates</button><button type="button" id="adoption-refresh">Refresh</button><span class="subtext">Up to 93 days · today is partial</span></form>
    <div class="spend-cards adoption-cards"><section class="card"><span>Human requests</span><strong>${spendCount(data.total_requests)}</strong><small>In the selected period</small></section><section class="card"><span>Active teammates</span><strong>${spendCount(data.active_users)}</strong><small>Distinct identified requesters</small></section><section class="card"><span>Last 7 complete days</span><strong>${spendCount(w.requests)}</strong><small>${esc(trend)}</small><small>${esc(w.start)} – ${esc(w.end)}</small></section></div>
    <section class="card spend-section"><div class="section-header"><h2>Requests per day</h2><div class="adoption-legend"><span>▮ Daily requests</span><span>━ 7-day average</span></div></div>${data.total_requests?'':'<p class="subtext">No recorded human requests in this period.</p>'}${adoptionChart(data.daily)}<p class="subtext">Previous week (${esc(w.previous_start)} – ${esc(w.previous_end)}): ${spendCount(w.previous_requests)} requests. Change: ${w.delta>0?'+':''}${spendCount(w.delta)}. Comparisons exclude today.</p></section>
    <details class="card spend-section"><summary>Daily breakdown</summary><div class="spend-table-wrap" role="region" aria-label="Daily adoption breakdown" tabindex="0"><table class="spend-table"><thead><tr><th>Date (UTC)</th><th>Requests</th><th>7-day average</th><th>Active teammates</th></tr></thead><tbody>${[...data.daily].reverse().map(d=>`<tr><td>${esc(d.date)}${d.partial?' · partial':''}</td><td>${spendCount(d.requests)}</td><td>${d.seven_day_average}</td><td>${spendCount(d.active_users)}</td></tr>`).join('')}</tbody></table></div></details>
    <details class="settings-hint"><summary>What counts as a request?</summary><p class="subtext adoption-definition">One request = one saved human chat submission, including initial prompts, follow-ups, and steering messages from Slack or the web. Submission retries count once. Includes queued, cancelled, failed, and subsequently deleted submissions; excludes demo sessions, delegated agents, and automation launch prompts. Human follow-ups to automations count. LLM calls and tool calls do not count.</p><p class="subtext adoption-definition">Uses retained message history; days without records appear as zero. Earlier non-chat tasks and purged history cannot be reconstructed. Linked Slack and Google accounts count as one teammate; anonymous requests are included in totals, not active teammates. The moving average includes the preceding six days, even outside the selected range. Trends show correlation, not whether a product change caused adoption to change.</p></details>`;
}
async function renderAdoption(){
  if(state.role!=='admin'){$('#content').innerHTML='<div class="error-banner">Adoption reports are available to organization administrators.</div>';return;}
  const version=state.pageVersion,request=++adoptionState.request;
  const query=new URLSearchParams();if(adoptionState.start)query.set('start',adoptionState.start);if(adoptionState.end)query.set('end',adoptionState.end);
  const data=await api('/api/admin/adoption?'+query);
  if(version!==state.pageVersion||request!==adoptionState.request)return;
  adoptionState.start=data.start;adoptionState.end=data.end;
  $('#content').innerHTML=adoptionDashboard(data);
  $('#adoption-filters').onsubmit=e=>{e.preventDefault();adoptionState.start=e.currentTarget.elements.start.value;adoptionState.end=e.currentTarget.elements.end.value;renderAdoption().catch(showError);};
  $('#adoption-refresh').onclick=()=>renderAdoption().catch(showError);
}
