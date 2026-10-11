// Organization-wide server observations; never infer timings from rendered chat text.
const performanceView = {hours:'24', kind:'all', model:'', sendKind:'all', request:0, owner:null, models:[]};

function performanceDuration(value) {
  if (value === null || value === undefined) return '—';
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`;
  return `${(value / 60000).toFixed(1)} min`;
}

function performanceCard(title, description, value, key) {
  return `<section class="card performance-card" data-performance-metric="${key}">
    <h2>${title}</h2><p class="subtext">${description}</p>
    <div class="performance-average">${performanceDuration(value.average_ms)}<span>average</span></div>
    <dl class="performance-percentiles"><div><dt>Median</dt><dd>${performanceDuration(value.p50_ms)}</dd></div>
      <div><dt>p95</dt><dd>${performanceDuration(value.p95_ms)}</dd></div><div><dt>Slowest</dt><dd>${performanceDuration(value.max_ms)}</dd></div></dl>
    <p class="performance-coverage">${value.count.toLocaleString()} measured · ${value.pending.toLocaleString()} waiting</p>
    ${value.incomplete || value.invalid ? `<p class="performance-missing">${value.incomplete} ended without this timing${value.invalid ? ` · ${value.invalid} invalid timestamps` : ''}</p>` : ''}
  </section>`;
}

function performanceTrend(data) {
  const series = [['startup','Startup to model'],['send_now','Send now pickup']];
  const all = data.trend.flatMap(row => series.map(([key]) => row[key].average_ms)).filter(value => value !== null);
  if (!all.length) return '';
  const max = Math.max(1, ...all), width = 840, height = 180;
  const x = index => 12 + index * (width - 24) / 23, y = value => height - 12 - value / max * (height - 24);
  const lines = series.map(([key,title]) => {
    const paths=[];let points=[];
    for (let i=0;i<data.trend.length;i++) {
      const value=data.trend[i][key].average_ms;
      if(value===null){if(points.length)paths.push(points);points=[];continue;}
      points.push(`${x(i)},${y(value)}`);
    }
    if(points.length)paths.push(points);
    return `${paths.filter(p=>p.length>1).map(p=>`<polyline class="performance-line ${key}" points="${p.join(' ')}"/>`).join('')}
      ${data.trend.map((row,i)=>row[key].average_ms===null?'':`<circle class="performance-point ${key}" cx="${x(i)}" cy="${y(row[key].average_ms)}" r="3"><title>${esc(title)}: ${performanceDuration(row[key].average_ms)} · ${row[key].count} samples</title></circle>`).join('')}`;
  }).join('');
  return `<section class="card performance-trend"><div class="performance-section-heading"><h2>Average latency over time</h2><span class="subtext">24 intervals · gaps mean no samples</span></div>
    <div class="performance-legend"><span class="startup">Startup to model</span><span class="send_now">Send now pickup</span><span>Scale: 0 – ${performanceDuration(max)}</span></div>
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Average startup and Send now latency. Exact interval values are available below."><line class="performance-axis" x1="12" y1="${height-12}" x2="${width-12}" y2="${height-12}"/>${lines}</svg>
    <div class="performance-chart-range"><span>${esc(new Date(data.start).toLocaleString())}</span><span>${esc(new Date(data.end).toLocaleString())}</span></div>
    <details><summary>View interval values</summary><div class="performance-table" tabindex="0" role="region" aria-label="Latency trend values"><table><thead><tr><th>Interval start</th><th>Startup average</th><th>Samples</th><th>Send now average</th><th>Samples</th></tr></thead><tbody>
      ${data.trend.map(row=>`<tr><td>${esc(new Date(row.start).toLocaleString())}</td><td>${performanceDuration(row.startup.average_ms)}</td><td>${row.startup.count}</td><td>${performanceDuration(row.send_now.average_ms)}</td><td>${row.send_now.count}</td></tr>`).join('')}
    </tbody></table></div></details></section>`;
}

function performanceResults(data) {
  const {metrics} = data;
  const any = metrics.first_model.total || metrics.send_now.total;
  return `${data.truncated.startup || data.truncated.send_now ? `<div class="settings-notice" role="status">High-volume window: statistics use the latest ${data.sample_limit.toLocaleString()} eligible requests for each measurement. Choose a shorter period to see a complete window.</div>` : ''}
    ${!any ? '<div class="card performance-empty"><h2>No timing samples in this view</h2><p>Timings begin with new web-chat requests after this release is installed. Existing chats are not backfilled. Try a wider period or reset the filters.</p><button class="quiet" id="performance-reset">Reset filters</button></div>' : ''}
    <div class="performance-metrics">
      ${performanceCard('Startup to model','Request received → first model request',metrics.first_model,'first_model')}
      ${performanceCard('Send now pickup','Send requested → applied or next turn claimed',metrics.send_now,'send_now')}
      ${performanceCard('Queue pickup','Request received → turn claimed',metrics.queue_pickup,'queue_pickup')}
      ${performanceCard('After pickup','Turn claimed → first model request',metrics.after_pickup,'after_pickup')}
    </div>
    ${performanceTrend(data)}
    ${any ? `<section class="card"><h2>By model</h2><div class="performance-table" tabindex="0" role="region" aria-label="Performance by model"><table><thead><tr><th>Model</th><th>Startup samples</th><th>Startup average</th><th>Startup p95</th><th>Send now samples</th><th>Send now average</th><th>Send now p95</th></tr></thead><tbody>${data.by_model.map(row=>`<tr><td>${esc(row.model)}</td><td>${row.startup.count}</td><td>${performanceDuration(row.startup.average_ms)}</td><td>${performanceDuration(row.startup.p95_ms)}</td><td>${row.send_now.count}</td><td>${performanceDuration(row.send_now.average_ms)}</td><td>${performanceDuration(row.send_now.p95_ms)}</td></tr>`).join('')}</tbody></table></div></section>
      <section class="card"><h2>Recent requests</h2><p class="subtext">Most recent 30 requests in this view. A dash means that milestone was not observed.</p><div class="performance-table" tabindex="0" role="region" aria-label="Recent performance requests"><table><thead><tr><th>Received</th><th>Conversation</th><th>Model</th><th>Status</th><th>Startup to model</th><th>Send now pickup</th></tr></thead><tbody>${data.recent.map(row=>`<tr><td>${esc(new Date(row.send_now_at||row.received_at).toLocaleString())}</td><td><a href="#run=${encodeURIComponent(row.run_id)}">${row.kind==='new_chat'?'New chat':'Follow-up'} · ${row.message_id}</a></td><td>${esc(row.model)}</td><td>${esc(row.status)}</td><td>${row.pickup_kind==='steering'?'Applied to active turn':performanceDuration(row.first_model_ms)}</td><td>${performanceDuration(row.send_now_ms)}${row.send_now_kind==='automatic'?'<span class="performance-auto">Automatic send</span>':''}</td></tr>`).join('')}</tbody></table></div></section>` : ''}
    <details class="card performance-method"><summary>What these numbers measure</summary>
      <p>Timings start when the web API handler receives a valid request. They include subsequent database, queue and workspace startup waits. They exclude upload time, browser-to-server transport, and browser rendering.</p>
      <p>Startup ends when Moyai records the first model request at the broker, before the provider responds. It is not time to first token or first visible answer. Queue pickup and after-pickup are parts of startup; do not add startup to them.</p>
      <p>Send now ends when the running agent acknowledges the message, or the message is claimed as a new turn. Choosing “Send now” on an older queued message starts a fresh measurement at that request, not at the original queue time. Repeated clicks retain the first request time. Automatic immediate sends are included unless filtered out.</p>
      <p>Startup excludes messages injected into an already running turn. Statistics cover main web conversations using cloud execution, across models and harnesses. Slack, scheduled messages, subagents and local demos are excluded. New chats and follow-ups are separate filters; a new chat is not necessarily a cold workspace.</p>
      <p>Averages and percentiles use observed milestones, including requests that later fail. Waiting requests, ended requests missing a milestone and invalid timestamps are counted separately. Missing observations never become zero latency. p95 uses nearest rank; median uses the midpoint. Very small samples can fluctuate.</p>
    </details>`;
}

async function renderPerformance() {
  const version=state.pageVersion, owner=state.userId;
  if(state.role!=='admin') {
    MoyaiUI.render($('#content'),'<section class="card"><h1>Performance</h1><p role="alert">Only administrators can view workspace performance.</p></section>');
    return;
  }
  if(performanceView.owner!==owner){Object.assign(performanceView,{hours:'24',kind:'all',model:'',sendKind:'all',models:[],owner});}
  MoyaiUI.render($('#content'),`<section class="performance-view"><div class="page-heading"><div><h1>Performance</h1><p class="subtext">Real startup and message-pickup latency across the workspace.</p></div><button class="quiet" id="performance-refresh">Refresh</button></div>
    <div class="performance-filters"><label>Period<select id="performance-hours">${[['1','Last hour'],['24','Last 24 hours'],['168','Last 7 days']].map(([value,label])=>`<option value="${value}" ${performanceView.hours===value?'selected':''}>${label}</option>`).join('')}</select></label>
      <label>Conversation<select id="performance-kind">${[['all','All conversations'],['new_chat','New chats'],['follow_up','Follow-ups']].map(([value,label])=>`<option value="${value}" ${performanceView.kind===value?'selected':''}>${label}</option>`).join('')}</select></label>
      <label>Model<select id="performance-model"><option value="">All models</option>${performanceView.models.map(model=>`<option value="${esc(model)}" ${performanceView.model===model?'selected':''}>${esc(model)}</option>`).join('')}</select></label>
      <label>Send now<select id="performance-send-kind">${[['all','Explicit + automatic'],['explicit','Explicit Send now'],['automatic','Automatic immediate']].map(([value,label])=>`<option value="${value}" ${performanceView.sendKind===value?'selected':''}>${label}</option>`).join('')}</select></label></div>
    <p id="performance-status" class="subtext" role="status"></p><div id="performance-results"></div></section>`);
  const active=()=>version===state.pageVersion&&state.view==='performance'&&state.userId===owner&&state.role==='admin';
  async function load(){
    const request=++performanceView.request;
    const expanded=[...document.querySelectorAll('#performance-results details[open]')].map(el=>el.querySelector('summary')?.textContent);
    performanceView.hours=$('#performance-hours').value;performanceView.kind=$('#performance-kind').value;
    performanceView.model=$('#performance-model').value;performanceView.sendKind=$('#performance-send-kind').value;
    $('#performance-refresh').disabled=true;$('#performance-status').textContent='Loading measurements…';
    MoyaiUI.render($('#performance-results'),'<p class="settings-loading">Loading performance…</p>');
    const query=new URLSearchParams({hours:performanceView.hours,kind:performanceView.kind,model:performanceView.model,send_kind:performanceView.sendKind});
    try{
      const data=await api('/api/admin/performance?'+query);
      if(!active()||request!==performanceView.request)return;
      performanceView.models=[...new Set([...performanceView.models,...data.models])].sort();
      const model=$('#performance-model'),selected=model.value;
      MoyaiUI.render(model,`<option value="">All models</option>${performanceView.models.map(name=>`<option value="${esc(name)}" ${name===selected?'selected':''}>${esc(name)}</option>`).join('')}`);
      MoyaiUI.render($('#performance-results'),performanceResults(data));
      for(const el of document.querySelectorAll('#performance-results details'))el.open=expanded.includes(el.querySelector('summary')?.textContent);
      $('#performance-status').textContent='Updated '+new Date(data.generated_at).toLocaleTimeString()+' · New web requests only · Refresh to update';
      if($('#performance-reset'))$('#performance-reset').onclick=()=>{for(const [id,value] of [['hours','24'],['kind','all'],['model',''],['send-kind','all']])$('#performance-'+id).value=value;load();};
    }catch(error){
      if(!active()||request!==performanceView.request)return;
      $('#performance-status').textContent='Measurements could not be loaded.';
      MoyaiUI.render($('#performance-results'),`<div class="card" role="alert"><h2>Unable to load performance</h2><p>${esc(error.message)}</p><button id="performance-retry">Try again</button></div>`);
      $('#performance-retry').onclick=load;
    }finally{if(active()&&request===performanceView.request)$('#performance-refresh').disabled=false;}
  }
  $('#performance-refresh').onclick=load;
  for(const id of ['hours','kind','model','send-kind'])$('#performance-'+id).onchange=load;
  await load();
}
