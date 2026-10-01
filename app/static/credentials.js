/* Values live only in the password input and the one HTTPS submission. */
function credentialScope(scope){return {session:'This session',personal:'Personal',organization:'Organization'}[scope]||scope;}
function renderCredentialRequests(requests){
  const target=document.querySelector('#credential-requests');if(!target)return;
  const signature=JSON.stringify(requests);if(target.dataset.signature===signature)return;target.dataset.signature=signature;
  target.innerHTML=requests.map(r=>`<section class="credential-request" aria-label="Provider key requested"><div><strong>${esc(r.provider_name)} API key needed</strong><p>${esc(r.reason)}</p><small>Provide it securely here. Never paste keys into chat or Slack.</small></div><div class="credential-actions">${r.can_personal||r.can_organization?`<button type="button" data-provide-key="${esc(r.id)}">Provide key</button>`:'<small>Waiting for the requester or an organization admin.</small>'}<a href="${esc(r.setup_url)}" target="_blank" rel="noopener noreferrer">Get a key ↗</a></div></section>`).join('');
  target.querySelectorAll('[data-provide-key]').forEach(button=>button.onclick=()=>openCredentialDialog(requests.find(r=>r.id===button.dataset.provideKey)).catch(showError));
}

async function renderSecrets(){
  const version=state.pageVersion,data=await api('/api/credentials');if(version!==state.pageVersion)return;
  $('#content').innerHTML=`<section class="secrets-page"><div class="section-header"><div><h1>Secrets</h1><p class="subtext">Saved API keys for your sessions and benchmarks.</p></div><button id="add-secret">Add key</button></div><p class="secret-sharing">Personal keys are available for your requests. Organization keys are shared with teammates and managed by admins. Session keys stay limited to that session and its subagents. Keys are never shown again; you can revoke access here.</p><div class="secret-list">${data.secrets.length?data.secrets.map(s=>`<article class="secret-card"><div><strong>${esc(s.label)}</strong><p>${esc(data.providers.find(p=>p.id===s.provider)?.name||s.provider)} <span class="secret-scope">${esc(credentialScope(s.scope))}</span></p><small>Added ${relative(s.created_at)}</small></div>${s.can_manage?`<button class="quiet" data-revoke-secret="${esc(s.id)}">Revoke access</button>`:'<small>Managed by an admin</small>'}</article>`).join(''):'<div class="card"><p>No saved keys yet.</p><p class="subtext">Moyai will ask through a secure form when a task needs one. You can also add a key now.</p></div>'}</div><p class="subtext">These provider charges are billed to the supplied key and are separate from Moyai’s gateway spend. Revoking access here does not delete the key at the provider.</p></section>`;
  $('#add-secret').onclick=()=>openCredentialDialog().catch(showError);
  document.querySelectorAll('[data-revoke-secret]').forEach(button=>button.onclick=()=>{
    if(button.dataset.confirm!=='yes'){button.dataset.confirm='yes';button.textContent='Confirm revoke';return;}
    button.disabled=true;api('/api/credentials/secrets/'+button.dataset.revokeSecret,{method:'DELETE'}).then(()=>renderSecrets()).catch(e=>{button.disabled=false;showError(e);});
  });
}

async function openCredentialDialog(request=null){
  const data=await api('/api/credentials'+(request?'?run_id='+encodeURIComponent(state.selected):''));
  const dialog=$('#credential-dialog'),form=$('#credential-form');
  const saved=request?data.secrets.filter(s=>s.provider===request.provider&&(s.scope==='organization'||request.can_personal)):[];
  const canPersonal=request?request.can_personal:true,admin=state.role==='admin';
  const providers=request?data.providers.filter(p=>p.id===request.provider):data.providers;
  form.innerHTML=`<button class="dialog-close" type="button" aria-label="Close key form">×</button><h2>${request?'Provide a key':'Save an API key'}</h2>${request?`<p class="subtext">${esc(request.reason)}</p>`:''}<div class="field"><label for="secret-provider">Provider</label><select id="secret-provider" ${request?'disabled':''}>${providers.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select></div><p><a id="secret-setup" target="_blank" rel="noopener noreferrer">Create or find a key at the provider ↗</a></p>${saved.length?`<div class="field"><label for="secret-source">Key to use</label><select id="secret-source"><option value="">Provide a new key</option>${saved.map(s=>`<option value="${esc(s.id)}">${esc(s.label)} · ${esc(credentialScope(s.scope))}</option>`).join('')}</select></div>`:''}<div id="secret-new-fields"><div class="field"><label for="secret-label">Name</label><input id="secret-label" maxlength="80" placeholder="Benchmark key" autocomplete="off"></div><div class="field"><label for="secret-value">API key</label><input id="secret-value" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="4096" required placeholder="Paste the key here"></div><div class="field"><label for="secret-scope">Who should be able to use this key?</label><select id="secret-scope" required><option value="" disabled selected>Choose who can use this key</option>${canPersonal?'<option value="personal">Personal · reuse for my requests</option>':''}<option value="organization" ${admin?'':'disabled'}>Organization · all teammates${admin?'':' (admin only)'}</option>${request&&canPersonal?'<option value="session">Only this session and its subagents</option>':''}</select></div></div><p class="secret-form-note">Stored encrypted and used for model calls to this provider. Keys stay out of chats and saved files. Results keep the session’s existing sharing. Provider usage is billed separately.</p><p id="secret-form-error" role="alert"></p><div class="credential-actions"><button type="submit">${request?'Use key and continue':'Save key'}</button>${request?'<button type="button" class="quiet" id="decline-secret">Continue without a key</button>':''}</div>`;
  const setup=()=>{$('#secret-setup').href=data.providers.find(p=>p.id===$('#secret-provider').value).setup_url;};setup();$('#secret-provider').onchange=setup;
  const clear=()=>{if($('#secret-value'))$('#secret-value').value='';form.reset();form.innerHTML='';};
  dialog.onclose=clear;dialog.oncancel=clear;
  form.querySelector('[aria-label="Close key form"]').onclick=()=>dialog.close();
  if($('#secret-source'))$('#secret-source').onchange=()=>{const existing=!!$('#secret-source').value;$('#secret-new-fields').hidden=existing;$('#secret-value').required=!existing;$('#secret-scope').required=!existing;$('#secret-scope').disabled=existing;$('#secret-value').value='';};
  const clientId=crypto.randomUUID();
  const resolve=async(decision)=>{
    const existing=$('#secret-source')?.value||'';
    if(decision==='provide'&&!existing&&!$('#secret-scope').value){$('#secret-form-error').textContent=request&&canPersonal?'Choose Personal, Organization, or This session before saving the key.':'Choose Personal or Organization before saving the key.';$('#secret-scope').focus();return;}
    const button=form.querySelector('[type="submit"]');button.disabled=true;
    if($('#decline-secret'))$('#decline-secret').disabled=true;
    const body=request?{decision,...(decision==='provide'?(existing?{secret_id:existing}:{scope:$('#secret-scope').value,label:$('#secret-label').value,value:$('#secret-value').value}):{})}:{provider:$('#secret-provider').value,label:$('#secret-label').value.trim()||data.providers.find(p=>p.id===$('#secret-provider').value).name+' key',scope:$('#secret-scope').value,value:$('#secret-value').value,client_id:clientId};
    $('#secret-value').value='';
    try{
      await api(request?'/api/credentials/requests/'+request.id:'/api/credentials/secrets',{method:'POST',body:JSON.stringify(body)});
      dialog.close();toast(request?'Request resolved. The session will resume.':'Key saved.');
      if(state.selected)await refreshChat(state.selected);else if(state.view==='secrets')await renderSecrets();
    }catch(error){if($('#secret-form-error'))$('#secret-form-error').textContent=error.message;button.disabled=false;if($('#decline-secret'))$('#decline-secret').disabled=false;}
    finally{body.value='';}
  };
  form.onsubmit=e=>{e.preventDefault();resolve('provide');};
  if($('#decline-secret'))$('#decline-secret').onclick=()=>resolve('decline');
  dialog.showModal();
}
