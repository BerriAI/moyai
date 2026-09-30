const operatingState={month:'',request:0};
const costProviders={modal:'Modal',temporal:'Temporal Cloud',render:'Render'};
const costLinks={modal:'https://modal.com/settings',temporal:'https://cloud.temporal.io/billing/invoices',render:'https://dashboard.render.com/'};

async function renderOperatingCosts(version){
  const request=++operatingState.request;
  const data=await api('/api/admin/operating-costs'+(operatingState.month?'?month='+encodeURIComponent(operatingState.month):''));
  if(version!==state.pageVersion||request!==operatingState.request||!$('#operating-costs'))return;
  const root=$('#operating-costs');
  if(!data.enabled){root.replaceChildren();return;}
  operatingState.month=data.month;
  root.innerHTML=`<section class="operating-overview" aria-label="Monthly operating costs">
    <div class="cost-heading"><div><h2>Operating costs</h2><p class="subtext">Monthly provider charges plus recorded model usage. USD, before tax.</p></div><form id="cost-month-form"><label>Billing month (UTC)<input type="month" name="month" required value="${esc(data.month)}"></label><button type="submit">View month</button></form></div>
    <div class="spend-cards cost-cards"><section class="card"><span>Infrastructure subtotal</span><strong>${dollars(data.known_infrastructure_cost)}</strong><small>${data.reported_providers} of 3 provider statements recorded</small></section><section class="card"><span>Recorded model usage</span><strong>${dollars(data.llm.recorded_cost)}</strong><small>All recorded keys · ${spendCount(data.llm.requests)} requests</small></section><section class="card cost-combined"><span>Known combined subtotal</span><strong>${dollars(data.known_combined_cost)}</strong><small>Before credits · includes the amounts shown here</small></section></div>
    <div class="cost-coverage" role="status"><strong>${data.reported_providers<3?'Incomplete infrastructure coverage':data.all_statements_final?'Provider statements finalized':'Provisional provider statements'}</strong><p>${data.reported_providers<3?'Missing providers have unknown costs. The subtotal includes only recorded amounts.':'Check each provider’s observation date; amounts may cover different portions of this month.'} ${data.llm.missing_costs?spendCount(data.llm.missing_costs)+' model requests have unknown costs.':''} Usage before model tracking began or outside Moyai is excluded.</p></div>
    <div class="spend-table-wrap"><table class="spend-table cost-table"><thead><tr><th>Provider / scope</th><th>Usage</th><th>Allocated fees</th><th>Credits applied</th><th>After credits</th><th></th></tr></thead><tbody>${data.providers.map(p=>{const s=p.statement;return `<tr><td><strong>${costProviders[p.provider]}</strong><small>${s?esc(s.scope):'No statement recorded'}</small>${s?`<small>${s.finalized?'Final':'Provisional'}${s.stale?' · Needs refresh':''} · checked ${esc(new Date(s.observed_at).toLocaleString())}</small>`:''}</td><td>${s?dollars(s.usage_cost):'Unknown'}</td><td>${s?dollars(s.allocated_fee):'Unknown'}</td><td>${s?.credits_applied!=null?dollars(s.credits_applied):'Unknown'}</td><td>${s?.net!=null?dollars(s.net):'Unknown'}</td><td><button class="small" data-cost-provider="${p.provider}">${s?'Review':'Add statement'}</button></td></tr>`;}).join('')}</tbody></table></div>
    <p class="subtext spend-note">Confirmed credits applied: <strong>${dollars(data.confirmed_credits)}</strong>. Known charges after credits: <strong>${data.known_combined_after_credits!==null?dollars(data.known_combined_after_credits):'Incomplete'}</strong>. Credit balances are not deducted until applied to these charges. This is a cost record, not proof of payment.</p>
    <details class="cost-explanation"><summary>What goes into these costs?</summary><p>Modal: sandbox compute, image builds, attributable storage and network charges. Temporal: actions, active/retained storage and support. Render: the Moyai service, disk, and any attributable bandwidth or build overages.</p><p>Workspace subscriptions can cover other apps. Enter only Moyai’s agreed share under allocated fees, with the allocation explained. Don’t include the same fee in usage. These shared costs are not assigned to individual users.</p><p>Provider figures are reviewed monthly. Modal Team/Enterprise exports can prefill scoped usage; Temporal and Render currently use administrator-entered statements. This view does not automatically detect plan changes or credit expiry. Recheck pricing and credits when plans change.</p><p>Model usage includes all keys recorded by Moyai during this calendar month. The per-user section below has its own date filter and describes the current key. ${data.llm.tracked_since?'Tracking began '+esc(new Date(data.llm.tracked_since).toLocaleString())+'.':''}</p></details>
    <dialog id="cost-dialog"></dialog>
  </section>`;
  $('#cost-month-form').onsubmit=e=>{e.preventDefault();operatingState.month=e.target.elements.month.value;renderOperatingCosts(version).catch(showError);};
  root.querySelectorAll('[data-cost-provider]').forEach(button=>button.onclick=()=>editCostStatement(data,button.dataset.costProvider,version));
}

function editCostStatement(data,provider,version){
  const s=data.providers.find(p=>p.provider===provider).statement;
  const dialog=$('#cost-dialog');
  const field=(label,name,value,extra='')=>`<label class="field cost-field">${label}<input name="${name}" value="${esc(value??'')}" ${extra}></label>`;
  dialog.innerHTML=`<form id="cost-statement-form"><button type="button" class="dialog-close" aria-label="Close">×</button><h2>${costProviders[provider]} · ${esc(data.month)}</h2><p>Use the provider’s billing statement for Moyai only. Saving replaces this month’s previous amount and preserves its audit history.</p><p><a href="${costLinks[provider]}" target="_blank" rel="noopener noreferrer">Open provider billing ↗</a></p>
    ${provider==='modal'?`<button type="button" id="cost-modal-preview" ${data.modal_import_ready?'':'disabled'}>Load Modal usage</button><p class="subtext spend-note">Requires Team/Enterprise billing access and an explicit Moyai object list. Prefill only; review before saving.</p>`:''}
    ${field('Resource scope','scope',s?.scope,'required maxlength="500" placeholder="Moyai app, namespace or service"')}
    <div class="cost-form-grid">${field('Usage charges (USD)','usage_cost',s?.usage_cost,'required type="number" step="any" min="0" max="10000000"')}${field('Allocated subscription / support fees (USD)','allocated_fee',s?.allocated_fee,'required type="number" step="any" min="0" max="10000000"')}</div>
    ${field('Allocation explanation','allocation_note',s?.allocation_note,'maxlength="1000" placeholder="Required when allocated fees are greater than zero"')}
    ${field('Credits actually applied (USD)','credits_applied',s?.credits_applied,'type="number" step="any" min="0" max="10000000" placeholder="Blank = unknown; enter 0 if confirmed none"')}
    ${field('Statement / invoice reference','source_reference',s?.source_reference,'required maxlength="500" placeholder="Reference only; don’t paste keys or payment details"')}
    ${field('Billing checked at (UTC)','observed_at',new Date(s?.observed_at||Date.now()).toISOString().slice(0,16),'required type="datetime-local"')}
    <label class="policy-check"><input type="checkbox" name="finalized" ${s?.finalized?'checked':''}> Final statement for a completed billing month</label><p class="subtext" id="cost-form-status" role="status"></p><button class="primary full" type="submit">Save statement</button>${s?'<button type="button" class="quiet full" id="cost-audit">View revision history</button><div id="cost-audit-content"></div>':''}</form>`;
  dialog.querySelector('.dialog-close').onclick=()=>dialog.close();
  const form=$('#cost-statement-form');
  form.onsubmit=async e=>{
    e.preventDefault();const button=form.querySelector('[type="submit"]');button.disabled=true;
    const values=Object.fromEntries(new FormData(form));
    const body={month:data.month,usage_cost:values.usage_cost,allocated_fee:values.allocated_fee,credits_applied:values.credits_applied||null,scope:values.scope,source_reference:values.source_reference,allocation_note:values.allocation_note,observed_at:new Date(values.observed_at+'Z').toISOString(),finalized:!!values.finalized,revision:s?.revision||0};
    try{await api('/api/admin/operating-costs/'+provider,{method:'PUT',body:JSON.stringify(body)});dialog.close();await renderOperatingCosts(version);toast('Monthly cost statement saved.');}catch(error){$('#cost-form-status').textContent=error.message;button.disabled=false;}
  };
  if($('#cost-modal-preview'))$('#cost-modal-preview').onclick=async e=>{
    const button=e.target;button.disabled=true;$('#cost-form-status').textContent='Fetching scoped usage…';
    try{const preview=await api('/api/admin/operating-costs/modal/preview',{method:'POST',body:JSON.stringify({month:data.month})});
      if(!preview.matched_object_ids.length){$('#cost-form-status').textContent='No matching resource rows. Verify resource IDs and reporting delay; no amount was filled.';return;}
      for(const name of ['usage_cost','scope','source_reference'])form.elements[name].value=preview[name];
      form.elements.observed_at.value=new Date(preview.observed_at).toISOString().slice(0,16);form.elements.finalized.checked=false;
      $('#cost-form-status').textContent=preview.note;
    }catch(error){$('#cost-form-status').textContent=error.message;}finally{button.disabled=false;}
  };
  if($('#cost-audit'))$('#cost-audit').onclick=async()=>{
    try{const rows=await api('/api/admin/operating-costs/audit/'+provider+'?month='+encodeURIComponent(data.month));$('#cost-audit-content').innerHTML=rows.map(r=>`<p class="subtext spend-note">Revision ${r.revision} · ${esc(r.actor_id)} · ${esc(new Date(r.created_at).toLocaleString())}<br>Usage ${dollars(r.payload.usage_cost)} · fees ${dollars(r.payload.allocated_fee)} · credits ${r.payload.credits_applied===null?'unknown':dollars(r.payload.credits_applied)}<br>${esc(r.payload.source_reference)}</p>`).join('');}catch(error){$('#cost-form-status').textContent=error.message;}
  };
  dialog.showModal();
}
