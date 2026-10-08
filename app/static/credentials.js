/* Secret values live only in this form and its HTTPS submission. */
let credentialDialogGeneration=0;
// Keep run links strict: credentials are identifiers, never values or arbitrary URLs.
function parseSessionLink(hash){
  if(typeof hash!=='string')return null;
  const fileLink=globalThis.MoyaiFiles?.sessionLink(hash);if(fileLink)return fileLink;
  const match=/^#run=([a-f0-9]{32})(?:&credential=([a-f0-9]{32})&generation=(0|[1-9][0-9]{0,14}))?$/.exec(hash);
  return match?{runId:match[1],credentialId:match[2]||'',generation:match[3]===undefined?null:Number(match[3]),hash}:null;
}
function resetCredentialNavigation(){
  state.credentialLink=null;state.credentialHighlight=null;state.credentialLinkNotice='';
  credentialDialogGeneration++;
  const dialog=document.querySelector('#credential-dialog');if(dialog?.open)dialog.close();
}
function credentialAvailability(scope){return {session:'Session only',personal:'Personal',organization:'Organization'}[scope]||'';}
const onePasswordAccess={
  provider:'generic',name:'1password-shared',format:'env',label:'1Password Shared',
  setup_url:'https://developer.1password.com/docs/service-accounts/get-started/',
  setup_instructions:'Use a dedicated Moyai service account with read and write access to the Shared vault only. For team access, choose Organization and Across future sessions. Manage vault permissions in 1Password.',
  input_fields:[{name:'OP_SERVICE_ACCOUNT_TOKEN',label:'Service account token',secret:true,required:true}]
};
function credentialScope(scope){return {session:'Personal',personal:'Personal',organization:'Organization'}[scope]||scope;}
function credentialLifetime(secret){return secret.lifetime||(secret.scope==='session'?'session':'persistent');}
function credentialStatus(secret){
  if(secret.status&&secret.status!=='active')return secret.status;
  if(secret.expires_at&&Date.parse(secret.expires_at)<=Date.now())return 'expired';
  return 'active';
}
function credentialSetupUrl(value){
  if(typeof value!=='string'||!value.startsWith('https://')||/[\s\\\x00-\x1f\x7f]/.test(value))return '';
  try{const url=new URL(value);return url.protocol==='https:'&&!url.username&&!url.password?url.href:'';}catch{return '';}
}
function credentialSetupInstructions(request){
  return request?.setup_instructions?`<details class="credential-setup-instructions"><summary>How to get access</summary><p>${esc(request.setup_instructions)}</p></details>`:'';
}
function credentialSetupLabel(url){
  return 'Setup guide · '+new URL(url).hostname+' ↗';
}
function credentialInputFields(request){
  if(!request||request.provider!=='generic'||request.format==='file')return [];
  if(request.input_fields?.length)return request.input_fields;
  if(request.name==='1password-shared')return onePasswordAccess.input_fields;
  // Compatibility for pending requests created before labeled inputs existed.
  const text=[request.name,request.reason,request.setup_instructions].join(' ');
  if(/\baws\b/i.test(text))return [
    {name:'AWS_ACCESS_KEY_ID',label:'Access key ID',secret:true,required:true},
    {name:'AWS_SECRET_ACCESS_KEY',label:'Secret access key',secret:true,required:true},
    {name:'AWS_SESSION_TOKEN',label:'Session token',secret:true,required:false},
    {name:/\bAWS_REGION\b/.test(text)&&!/\bAWS_DEFAULT_REGION\b/.test(text)?'AWS_REGION':'AWS_DEFAULT_REGION',label:'Region',secret:false,required:true}
  ];
  const names=[...new Set(text.match(/\b[A-Z][A-Z0-9_]*(?:_TOKEN|_API_KEY|_ACCESS_TOKEN)\b/g)||[])];
  return names.length===1?[{name:names[0],label:'Access token',secret:true,required:true}]:[];
}
function credentialMatches(secret,request,rootId){
  if(secret.provider!==request.provider||credentialStatus(secret)!=='active')return false;
  if(secret.scope!=='organization'&&!request.can_personal)return false;
  if(credentialLifetime(secret)==='session'&&secret.root_id!==rootId)return false;
  return request.provider!=='generic'||(secret.name===request.name&&secret.format===request.format&&(secret.env_var||'')===(request.env_var||''));
}
function renderCredentialRequests(requests){
  const target=document.querySelector('#credential-requests');if(!target)return;
  const link=state.credentialLink;
  let focusId='';
  if(link&&link.runId===state.selected&&link.pageVersion===state.pageVersion){
    // Consume on the first complete session response, including unavailable targets.
    // A later poll or browser refresh must not revive a stale Slack link.
    state.credentialLink=null;
    history.replaceState(null,'','#run='+link.runId);
    const request=requests.find(r=>r.id===link.credentialId&&r.generation===link.generation);
    if(request&&(request.can_personal||request.can_organization)){
      state.credentialHighlight={id:request.id,generation:request.generation};focusId=request.id;
    }else state.credentialLinkNotice=request?'Only the requester or an organization admin can provide this secret.':'This access link is no longer current. The request may have been resolved or replaced. '+(requests.length?'Check the pending requests below.':'There are no pending access requests in this session.');
  }
  if(state.credentialHighlight&&!requests.some(r=>r.id===state.credentialHighlight.id&&r.generation===state.credentialHighlight.generation)){
    state.credentialHighlight=null;state.credentialLinkNotice='This access request has been resolved or is no longer available.';
  }
  const signature=JSON.stringify([requests,state.credentialHighlight,state.credentialLinkNotice]);if(target.dataset.signature===signature)return false;target.dataset.signature=signature;
  MoyaiUI.render(target, (state.credentialLinkNotice?`<p class="credential-link-notice" role="status">${esc(state.credentialLinkNotice)}</p>`:'')+requests.map(r=>{
    const generic=r.provider==='generic';
    const failure={expired:'Saved access expired.',invalid:'Saved access needs updating.',permission:'Saved access needs additional permissions.'}[r.failure]||'';
    const fields=credentialInputFields(r),name=fields.length===1?fields[0].name:generic?r.name:(r.provider_name||r.provider)+' API key';
    const highlighted=state.credentialHighlight?.id===r.id&&state.credentialHighlight.generation===r.generation,availability=credentialAvailability(r.preferred_scope);
    return `<section class="credential-request${highlighted?' credential-request-linked':''}" data-credential-request="${esc(r.id)}" aria-label="Credentials requested"><div class="credential-request-heading"><span aria-hidden="true">⚿</span><span>Credentials requested: <strong>${esc(name)}</strong></span></div>${failure?`<p class="credential-request-failure">${esc(failure)}</p>`:''}${availability?`<p class="credential-request-scope">Availability: ${esc(availability)}</p>`:''}${highlighted?`<p class="credential-link-instruction" id="credential-link-instruction" role="status">Select Provide Secret below to enter this credential securely.</p>`:''}<div class="credential-request-footer">${r.can_personal||r.can_organization?`<button type="button" data-provide-key="${esc(r.id)}" aria-haspopup="dialog" aria-controls="credential-dialog"${highlighted?' aria-describedby="credential-link-instruction"':''}>Provide Secret</button>`:'<small>Waiting for the requester or an organization admin.</small>'}</div></section>`;
  }).join(''));
  target.querySelectorAll('[data-provide-key]').forEach(button=>button.onclick=()=>{
    const request=requests.find(r=>r.id===button.dataset.provideKey);
    state.credentialHighlight=null;renderCredentialRequests(requests);
    openCredentialDialog(request).catch(showError);
  });
  if(focusId&&!document.querySelector('#credential-dialog')?.open){
    const button=target.querySelector(`[data-provide-key="${focusId}"]`);
    button?.closest('.credential-request').scrollIntoView({block:'center',behavior:'instant'});
    button?.focus({preventScroll:true});
    return !!button;
  }
  return false;
}

async function renderSecrets(){
  const version=state.pageVersion,data=await api('/api/credentials');if(version!==state.pageVersion)return;
  MoyaiUI.render($('#content'), `<section class="secrets-page">
    <div class="section-header"><div><h1>Secrets</h1><p class="subtext">Manage the service access Moyai can use.</p></div><div class="credential-actions"><button id="add-onepassword">Connect 1Password</button><button class="primary" id="add-secret">Add credential</button></div></div>
    <details class="settings-hint"><summary>Who can use saved credentials?</summary><p>Personal credentials are available for your requests. Organization credentials are shared with your team and managed by admins. Session access stays limited to that session and its subagents. Saved values are never shown again.</p></details>
    <div class="settings-toolbar"><input type="search" id="secret-search" aria-label="Search credentials" placeholder="Search by name or service"><select id="secret-scope-filter" aria-label="Filter credentials by scope"><option value="">All scopes</option><option value="personal">Personal</option><option value="organization">Organization</option></select><span id="secret-count" class="settings-result-count" role="status"></span></div>
    <div class="settings-empty" id="secret-no-results" hidden><h2>No matching credentials</h2><p>Try another name or clear the filters.</p><button>Clear filters</button></div>
    <div class="secret-list">${data.secrets.length?data.secrets.map(s=>{
      const status=credentialStatus(s),generic=s.provider==='generic';
      return `<article class="secret-card" data-filter="${s.scope==='organization'?'organization':'personal'}"><div><strong>${esc(s.label)}</strong><p><span class="secret-reference">${esc(generic?s.name:(data.providers.find(p=>p.id===s.provider)?.name||s.provider))}</span></p><p><span class="secret-scope">${esc(credentialScope(s.scope))}</span><span class="secret-scope">${credentialLifetime(s)==='session'?'This session only':'Future sessions'}</span><span class="secret-scope ${status==='active'?'healthy':'settings-status-warning'}">${esc({active:'Active',expired:'Expired',invalid:'Needs updating'}[status]||status)}</span></p><small>${generic?(s.format==='file'?'Credential file':'Environment variables')+' · ':''}Added ${relative(s.created_at)}${s.expires_at?' · Expires '+esc(new Date(s.expires_at).toLocaleString()):''}</small>${generic&&s.format==='file'&&s.env_var?`<small> · ${esc(s.env_var)}</small>`:''}${status!=='active'?'<p class="subtext">Update this credential before Moyai can use it again.</p>':''}</div>${s.can_manage?`<div class="credential-actions"><button data-edit-secret="${esc(s.id)}">${status==='active'?'Edit':'Update access'}</button><button class="quiet danger" data-revoke-secret="${esc(s.id)}">Revoke access</button></div>`:'<small>Managed by an admin</small>'}</article>`;
    }).join(''):'<div class="settings-empty"><h2>No saved credentials yet</h2><p>Add a credential above, or Moyai will ask through a secure form when a task needs access.</p></div>'}</div>
    <p class="subtext settings-library-note">Revoking access here does not revoke the credential at its provider. Provider charges are billed separately from Moyai’s gateway spend.</p></section>`);
  bindSettingsFilter({input:'#secret-search',select:'#secret-scope-filter',rows:'.secret-card',count:'#secret-count',empty:'#secret-no-results'});
  $('#add-secret').onclick=()=>openCredentialDialog().catch(showError);
  $('#add-onepassword').onclick=()=>openOnePasswordDialog().catch(showError);
  document.querySelectorAll('[data-edit-secret]').forEach(button=>button.onclick=()=>openCredentialDialog(null,data.secrets.find(s=>s.id===button.dataset.editSecret)).catch(showError));
  document.querySelectorAll('[data-revoke-secret]').forEach(button=>button.onclick=async()=>{
    const secret=data.secrets.find(s=>s.id===button.dataset.revokeSecret);
    if(!await confirmSettingsAction('Revoke saved access?', `Future tasks cannot use “${secret.label}”. This does not revoke the credential at its provider.`, 'Revoke access'))return;
    button.disabled=true;
    try { await api('/api/credentials/secrets/'+button.dataset.revokeSecret,{method:'DELETE'});if(version===state.pageVersion)await renderSecrets(); }
    catch(e){button.disabled=false;showError(e);}
  });
}

async function openOnePasswordDialog(){
  const generation=++credentialDialogGeneration;
  const data=await api('/api/credentials');
  if(generation!==credentialDialogGeneration)return;
  const saved=data.secrets.filter(s=>s.provider==='generic'&&s.name==='1password-shared'&&s.format==='env');
  if(saved.length>1)throw new Error('Multiple Shared connections exist. Edit the intended connection from Secrets.');
  if(saved.length&&!saved[0].can_manage)throw new Error('Shared access is already saved and managed by an administrator.');
  return openCredentialDialog(null,saved[0]||null,onePasswordAccess);
}

async function openCredentialDialog(request=null,secret=null,preset=null){
  const generation=++credentialDialogGeneration,page=state.pageVersion,runId=state.selected,dialog=$('#credential-dialog'),form=$('#credential-form');
  if(dialog.open)dialog.close();
  const data=await api('/api/credentials'+(request?'?run_id='+encodeURIComponent(state.selected):''));
  if(generation!==credentialDialogGeneration||page!==state.pageVersion||runId!==state.selected)return;
  // Refresh manager metadata so an already open page cannot overwrite a newer revision.
  if(secret){secret=data.secrets.find(s=>s.id===secret.id);if(!secret?.can_manage)throw new Error('This credential is no longer available to edit.');}
  const rootId=request?(data.root_id||request.root_id||state.selected):(secret?.root_id||'');
  const saved=request?data.secrets.filter(s=>credentialMatches(s,request,rootId)):[];
  const canPersonal=request?request.can_personal:true,admin=state.role==='admin';
  const canOrganization=request?!!request.can_organization:admin;
  const providerList=data.providers.some(p=>p.id==='generic')?data.providers:[...data.providers,{id:'generic',name:'Other service',setup_url:''}];
  if(!preset&&secret?.provider==='generic'&&secret.name==='1password-shared'&&secret.format==='env')preset=onePasswordAccess;
  const fixed=request||secret||preset,providers=fixed?providerList.filter(p=>p.id===fixed.provider):providerList;
  const existingScope=secret?(secret.scope==='session'?'personal':secret.scope):'';
  const existingLifetime=secret?credentialLifetime(secret):'';
  const inputFields=credentialInputFields(request||preset);
  const useChoice=credentialAvailability(request?.preferred_scope)?request.preferred_scope:(canPersonal?'personal':'');
  const choiceAllowed=useChoice==='organization'?canOrganization:!!useChoice&&canPersonal;
  const useDescriptions={session:'Only for this session and its subagents. Not reused in other sessions.',personal:'Only you, across future sessions.',organization:'Everyone in your organization, across future sessions.'};
  const useDescription=choiceAllowed?useDescriptions[useChoice]:useChoice==='organization'?'Organization was selected in Slack. An organization admin must provide it, or you can choose another option.':useChoice?'Personal access was selected in Slack. Only the requester can provide it, or you can choose Organization.':'Choose Organization to share access with your organization.';
  const useSelector=request?`<fieldset class="secret-use-field"><legend>Who can use this secret?</legend><div class="secret-use-selector" role="radiogroup" aria-label="Secret availability"><span class="secret-use-highlight" aria-hidden="true"></span>${[['session','Session only'],['personal','Personal'],['organization','Organization']].map(([value,label])=>`<label><input id="secret-use-${value}" type="radio" name="secret-use" value="${value}" ${value===useChoice?'checked':''} ${(value==='organization'?!canOrganization:!canPersonal)?'disabled':''}><span>${label}</span></label>`).join('')}</div><p id="secret-use-description" class="secret-use-description">${useDescription}</p></fieldset><input id="secret-scope" type="hidden" value="${choiceAllowed?(useChoice==='organization'?'organization':'personal'):''}"><input id="secret-lifetime" type="hidden" value="${choiceAllowed?(useChoice==='session'?'session':'persistent'):''}">`:'';
  const localExpiry=secret?.expires_at?new Date(Date.parse(secret.expires_at)-new Date(secret.expires_at).getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  const labelField=`<div class="field"><label for="secret-label">Display name</label><input id="secret-label" maxlength="80" placeholder="My service access" autocomplete="off" value="${esc(secret?.label||preset?.label||'')}"></div>`,expiryField=`<div class="field"><label for="secret-expiry">Provider expiry (optional, your local time)</label><input id="secret-expiry" type="datetime-local" value="${esc(localExpiry)}"><small>Leave blank if unknown or if the credential does not expire.</small></div>`;
  const requestContext=request?`<p class="credential-context">${esc(inputFields.length===1?inputFields[0].name:request.provider==='generic'?request.name:(request.provider_name||providers[0]?.name||request.provider)+' API key')}</p>`:'';
  MoyaiUI.render(form, `<button class="dialog-close" type="button" aria-label="Close credential form">×</button><h2 id="credential-dialog-title">${secret?'Edit saved access':request?'Provide Secret':preset?'Connect 1Password Shared':'Save a credential'}</h2>${preset?`<p class="subtext">${esc(preset.setup_instructions)}</p>`:""}${requestContext}${request?`<details class="credential-request-details"><summary>Why is this needed?</summary><p class="subtext credential-request-reason">${esc(request.reason)}</p>${credentialSetupInstructions(request)}</details>`:''}<div id="secret-provider-field" class="field" ${request||preset?'hidden':''}><label for="secret-provider">Service</label><select id="secret-provider" ${fixed?'disabled':''}>${providers.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select></div><p id="secret-setup-row"><a id="secret-setup" target="_blank" rel="noopener noreferrer">Set up access ↗</a></p>${saved.length?`<div class="field"><label for="secret-source">Credential to use</label><select id="secret-source"><option value="">Provide a new credential</option>${saved.map(s=>`<option value="${esc(s.id)}">${esc(s.label)} · ${esc(credentialScope(s.scope))} · ${credentialLifetime(s)==='session'?'This session only':'Future sessions'}</option>`).join('')}</select></div>`:''}<div id="secret-new-fields"><div id="secret-generic-fields"><div class="field"><label for="secret-name">Service or capability name</label><input id="secret-name" maxlength="80" placeholder="production-cluster" autocomplete="off" value="${esc(fixed?.name||'')}" ${fixed?'disabled':''}></div><div class="field"><label for="secret-format">Credential format</label><select id="secret-format" ${fixed?'disabled':''}><option value="env" ${fixed?.format==='file'?'':'selected'}>Environment variables (JSON)</option><option value="file" ${fixed?.format==='file'?'selected':''}>Credential file</option></select></div><div id="secret-env-var-field" class="field"><label for="secret-env-var">Environment variable for the file path</label><input id="secret-env-var" maxlength="128" pattern="[A-Z_][A-Z0-9_]*" placeholder="KUBECONFIG" autocomplete="off" value="${esc(fixed?.env_var||'')}" ${fixed?'disabled':''}></div></div>${request?'':labelField}<div id="secret-value-fields"></div>${request?useSelector:`<div><div class="field"><label for="secret-scope">Who can use it?</label><select id="secret-scope" required><option value="" disabled ${secret?'':'selected'}>${request?'Choose who can use it':'Choose a scope'}</option>${canPersonal?`<option value="personal" ${existingScope==='personal'?'selected':''}>Personal · my requests</option>`:''}<option value="organization" ${existingScope==='organization'?'selected':''} ${canOrganization?'':'disabled'}>Organization · everyone${canOrganization?'':' (admin only)'}</option></select></div><div class="field"><label for="secret-lifetime">When can Moyai use it?</label><select id="secret-lifetime" required><option value="" disabled ${secret?'':'selected'}>${request?'Choose when to use it':'Choose when it can be used'}</option><option value="session" ${existingLifetime==='session'?'selected':''}>This session only</option><option value="persistent" ${existingLifetime==='persistent'?'selected':''}>Across future sessions</option></select></div></div>`}${request?'':`<div id="secret-root-field" class="field" hidden><label for="secret-root">Session to allow</label><select id="secret-root" disabled><option value="" disabled ${rootId?'':'selected'}>Choose a session</option>${rootId?`<option value="${esc(rootId)}" selected>Saved session · ${esc(rootId.slice(0,8))}</option>`:''}</select><small id="secret-root-note"></small><button id="secret-root-reload" type="button" class="quiet">Reload sessions</button></div>`}${request?'':expiryField}</div><p id="secret-form-note" class="secret-form-note"></p><p id="secret-form-error" role="alert"></p><div class="credential-actions"><button type="submit">${secret?'Save changes':request?'Submit':'Save credential'}</button>${request?'<button type="button" class="quiet" id="decline-secret">Continue without this access</button>':''}</div>`);
  let uploadGeneration=0;
  const clearValue=()=>{uploadGeneration++;if($('#secret-value'))$('#secret-value').value='';if($('#secret-file'))$('#secret-file').value='';inputFields.forEach((field,i)=>{const input=$('#secret-input-'+i);if(input)input.value='';});};
  const clear=()=>{clearValue();form.reset();MoyaiUI.render(form, '');};
  dialog.onclose=clear;dialog.oncancel=clear;
  form.querySelector('[aria-label="Close credential form"]').onclick=()=>dialog.close();
  if(request){
    for(const [value,scope,lifetime,description] of [
      ['session','personal','session','Only for this session and its subagents. Not reused in other sessions.'],
      ['personal','personal','persistent','Only you, across future sessions.'],
      ['organization','organization','persistent','Everyone in your organization, across future sessions.']
    ])$('#secret-use-'+value).onchange=()=>{
      if(!$('#secret-use-'+value).checked||$('#secret-use-'+value).disabled)return;
      $('#secret-scope').value=scope;$('#secret-lifetime').value=lifetime;$('#secret-use-description').textContent=description;
    };
  }
  const sourceChanged=()=>{
    const existing=!!$('#secret-source')?.value,generic=$('#secret-provider').value==='generic',file=generic&&$('#secret-format').value==='file';
    $('#secret-new-fields').hidden=existing;
    for(const id of ['secret-scope','secret-lifetime']){$('#'+id).required=!existing;$('#'+id).disabled=existing;}
    document.querySelectorAll?.('[name="secret-use"]').forEach(input=>input.disabled=existing||(input.value==='organization'?!canOrganization:!canPersonal));
    if($('#secret-value')){$('#secret-value').required=!existing&&!secret;$('#secret-value').disabled=existing;}
    inputFields.forEach((field,i)=>{const input=$('#secret-input-'+i);input.required=!existing&&!secret&&field.required!==false;input.disabled=existing;});
    if($('#secret-expiry'))$('#secret-expiry').disabled=existing;
    $('#secret-name').required=generic&&!existing&&!fixed;
    $('#secret-env-var').required=file&&!existing&&!fixed;
    clearValue();
  };
  const drawValue=()=>{
    clearValue();
    const generic=$('#secret-provider').value==='generic',file=generic&&$('#secret-format').value==='file';
    $('#secret-generic-fields').hidden=!generic||!!request||!!preset;$('#secret-env-var-field').hidden=!file;
    // Hidden fields must not participate in native form validation.
    $('#secret-name').disabled=!generic||!!fixed;$('#secret-format').disabled=!generic||!!fixed;$('#secret-env-var').disabled=!file||!!fixed;
    const title=secret?'Replacement credential (optional)':generic?(file?'Credential file contents':'Environment variables (JSON)'):'Access token';
    MoyaiUI.render($('#secret-value-fields'), `<div class="field"><label for="secret-value">${title}</label>${generic?`<textarea id="secret-value" rows="${request?4:6}" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="131072" ${secret?'':'required'} placeholder="${file?'Paste the file contents here':'{&quot;SERVICE_TOKEN&quot;: &quot;your-token&quot;}'}"></textarea>`:`<input id="secret-value" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="4096" ${secret?'':'required'} placeholder="${secret?'Leave blank to keep the saved value':'Paste the key here'}">`}${file?'<label for="secret-file">Or choose a credential file</label><input id="secret-file" type="file" autocomplete="off">':''}<small>${secret?'Leave the replacement blank to keep the current value. ':''}${generic?(file?'File contents are sent securely and made available only while the authorized command runs.':'Use a JSON object mapping environment variable names to string values.'):''}</small></div>`);
    if(inputFields.length)MoyaiUI.render($('#secret-value-fields'), inputFields.map((field,i)=>`<div class="field"><label for="secret-input-${i}">${esc(inputFields.length===1&&field.secret!==false?'Access token':field.label)}${field.required===false?' (optional)':''}</label><input id="secret-input-${i}" type="${field.secret===false?'text':'password'}" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="131072" ${field.required===false||secret?'':'required'} placeholder="${secret?'Leave blank to keep the saved value':field.secret===false?'Enter '+esc(field.label.toLowerCase()):'Paste here'}"></div>`).join(''));
    const setup=credentialSetupUrl(request?.setup_url||preset?.setup_url||providers.find(p=>p.id===$('#secret-provider').value)?.setup_url);
    $('#secret-setup-row').hidden=!setup;
    if(setup){$('#secret-setup').href=setup;$('#secret-setup').textContent=credentialSetupLabel(setup);}
    else $('#secret-setup').removeAttribute('href');
    $('#secret-form-note').textContent=request?'Stored encrypted. The value is never shown in chat.':generic?'Stored encrypted. Approved commands can use this access in the sandbox. Sharing access does not reveal the saved credential. Session-only use does not change the provider’s expiry.':'Stored encrypted and used for model calls to this provider. Keys stay out of the sandbox. Sharing access does not reveal the saved key. Provider usage is billed separately.';
    if($('#secret-file'))$('#secret-file').onchange=async event=>{
      const input=event.target,file=input.files?.[0],generation=++uploadGeneration;if(!file)return;
      $('#secret-value').value='';
      try{
        if(file.size>131072)throw new Error('Choose a UTF-8 credential file of at most 128 KB.');
        const value=await file.text();
        if(generation!==uploadGeneration||!dialog.open)return;
        if(value.length>131072||value.includes('\0'))throw new Error('Choose a UTF-8 text credential file of at most 128 KB.');
        $('#secret-value').value=value;$('#secret-form-error').textContent='';
      }catch(error){if(generation===uploadGeneration&&$('#secret-form-error'))$('#secret-form-error').textContent=error.message;}
      finally{input.value='';}
    };
    sourceChanged();
  };
  drawValue();$('#secret-provider').onchange=drawValue;$('#secret-format').onchange=drawValue;
  if($('#secret-expiry'))$('#secret-expiry').oninvalid=()=>{};
  if($('#secret-source'))$('#secret-source').onchange=sourceChanged;
  let sessions=null,sessionLoad=null;
  const lifetimeChanged=async(reload=false)=>{
    if(request)return;
    const field=$('#secret-root-field'),picker=$('#secret-root'),note=$('#secret-root-note'),reloadButton=$('#secret-root-reload');
    const sessionOnly=$('#secret-lifetime').value==='session';
    field.hidden=!sessionOnly;picker.required=sessionOnly;picker.disabled=!sessionOnly;
    if(!sessionOnly)return;
    if(reload)sessions=null;
    const drawSessions=()=>{
      const selected=picker.value,scope=$('#secret-scope').value;
      const choices=(sessions||[]).filter(run=>!run.parent_run_id&&run.chat_enabled!==false&&(scope!=='personal'||!state.userId||!run.active_user_id||run.active_user_id===state.userId||(state.userId.startsWith('google:')&&run.active_user_id.startsWith('slack:'))));
      // A saved root is retained even outside the latest page. Authorization is checked on save.
      if(rootId&&!choices.some(run=>run.id===rootId))choices.unshift({id:rootId,prompt:'Saved session'});
      const chosen=choices.some(run=>run.id===selected)?selected:'';
      MoyaiUI.render(picker, '<option value="" disabled'+(chosen?'':' selected')+'>Choose a session</option>'+choices.map(run=>`<option value="${esc(run.id)}" ${run.id===chosen?'selected':''}>${esc(sessionTitle(run))} · ${esc(run.id.slice(0,8))}</option>`).join(''));
      picker.value=chosen;
      note.textContent=choices.length?(scope==='personal'?'Choose a chat where you are the requester. Access is checked again when you save.':'Access is limited to the chosen session and its subagents.'):'No matching sessions found. Start a chat, then reload sessions.';
    };
    if(sessions){drawSessions();return;}
    note.textContent='Loading sessions…';reloadButton.disabled=true;
    if(!sessionLoad)sessionLoad=api('/api/runs'+(rootId?'?focus='+encodeURIComponent(rootId):''));
    try{
      const rows=await sessionLoad;
      if(generation!==credentialDialogGeneration||!dialog.open)return;
      sessions=rows;drawSessions();
    }catch(error){if(generation===credentialDialogGeneration&&dialog.open)note.textContent='Could not load sessions. '+error.message;}
    finally{sessionLoad=null;reloadButton.disabled=false;}
  };
  if(!request){$('#secret-lifetime').onchange=()=>lifetimeChanged();$('#secret-scope').onchange=()=>lifetimeChanged();$('#secret-root-reload').onclick=()=>lifetimeChanged(true);}
  const clientId=crypto.randomUUID();
  const resolve=async decision=>{
    const existing=$('#secret-source')?.value||'',isNew=decision==='provide'&&!existing;
    if(isNew&&!$('#secret-scope').value){$('#secret-form-error').textContent='Choose Personal or Organization before saving the credential.';$(request?'#secret-use-organization':'#secret-scope').focus();return;}
    if(isNew&&!$('#secret-lifetime').value){$('#secret-form-error').textContent='Choose when Moyai can use this credential before saving.';$('#secret-lifetime').focus();return;}
    const chosenRoot=request?rootId:($('#secret-root')?.value||'');
    if(isNew&&!request&&$('#secret-lifetime').value==='session'&&!chosenRoot){$('#secret-form-error').textContent='Choose a session for session-only access before saving.';$('#secret-root').focus();return;}
    const expiry=isNew&&$('#secret-expiry')?$('#secret-expiry').value:'';
    if(expiry&&!Number.isFinite(Date.parse(expiry))){$('#secret-expiry').oninvalid();$('#secret-form-error').textContent='Enter a valid provider expiry.';$('#secret-expiry').focus();return;}
    const fields=isNew?{scope:$('#secret-scope').value,lifetime:$('#secret-lifetime').value,label:$('#secret-label')?.value.trim()||'',expires_at:secret&&expiry===localExpiry?(secret.expires_at||''):(expiry?new Date(expiry).toISOString():'')}:{};
    if(isNew&&inputFields.length){
      const values=Object.create(null);
      const replacing=!secret||inputFields.some((field,i)=>$('#secret-input-'+i).value);
      for(const [i,field] of inputFields.entries()){
        const input=$('#secret-input-'+i);
        if(replacing&&field.required!==false&&!input.value.trim()){$('#secret-form-error').textContent='Enter '+field.label.toLowerCase()+'.';input.focus();return;}
        if(input.value)values[field.name]=input.value;
      }
      if(replacing)fields.value=JSON.stringify(values);
    }else if(isNew&&(!secret||$('#secret-value').value))fields.value=$('#secret-value').value;
    if(!request&&fields.lifetime==='session')fields.root_id=chosenRoot;
    const provider=$('#secret-provider').value;
    const body=secret?{revision:secret.revision,...fields}:request?{decision,generation:request.generation||0,...(decision==='provide'?(existing?{secret_id:existing}:fields):{})}:{provider,...fields,client_id:clientId,...(provider==='generic'?{name:$('#secret-name').value.trim(),format:$('#secret-format').value,...($('#secret-format').value==='file'?{env_var:$('#secret-env-var').value.trim()}:{} )}:{})};
    if(!request&&!secret&&!body.label)body.label=provider==='generic'?(body.name||'Service access'):providers.find(p=>p.id===provider).name+' key';
    const button=form.querySelector('[type="submit"]');button.disabled=true;
    if($('#decline-secret'))$('#decline-secret').disabled=true;
    clearValue();
    try{
      await api(secret?'/api/credentials/secrets/'+secret.id:request?'/api/credentials/requests/'+request.id:'/api/credentials/secrets',{method:secret?'PATCH':'POST',body:JSON.stringify(body)});
      if(generation!==credentialDialogGeneration||!dialog.open)return;
      dialog.close();toast(secret?'Saved access updated.':request?'Request resolved. The session will resume.':'Credential saved.');
      if(request&&state.selected)await refreshChat(state.selected);else if(state.view==='secrets')await renderSecrets();
    }catch(error){if(generation===credentialDialogGeneration&&dialog.open){if($('#secret-form-error'))$('#secret-form-error').textContent=error.message;button.disabled=false;if($('#decline-secret'))$('#decline-secret').disabled=false;}}
    finally{if('value' in body)body.value='';if('value' in fields)fields.value='';}
  };
  form.onsubmit=e=>{e.preventDefault();return resolve('provide');};
  if($('#decline-secret'))$('#decline-secret').onclick=()=>resolve('decline');
  dialog.showModal();
  if(!request&&existingLifetime==='session')await lifetimeChanged();
}
