/* Reusable organization project environments. Scripts are editable only by admins. */
let environmentRefresh;
function environmentOptions(items, selected='auto') {
  return `<option value="auto" ${selected==='auto'?'selected':''}>Automatic · repository or workspace default</option><option value="none" ${selected==='none'?'selected':''}>Base tools only</option>` + items.map(item=>`<option value="${esc(item.id)}" ${selected===item.id?'selected':''}>${esc(item.name)}${item.active_build?'':' · prepare on first use'}</option>`).join('');
}
function environmentCard(item) {
  const build=item.builds[0], busy=build&&!['ready','failed'].includes(build.phase);
  return `<article class="environment-card"><div class="environment-card-top"><div><span class="environment-glyph" aria-hidden="true">▧</span><h2>${esc(item.name)}</h2><p>${esc(item.repository)}</p></div><span class="badge ${item.active_build?'env-ready':''}">${item.active_build?'Ready':busy?'Setting up':item.activate_on_ready?'On first use':'Not built'}</span></div>
    <div class="environment-card-state"><span>${item.is_default?'Workspace default':item.enabled?'Available for sessions':item.activate_on_ready?'Automatic · repository match':'Not enabled'}</span><span>Recipe v${item.revision}</span></div>
    ${build?`<div class="environment-build-state ${build.phase==='failed'?'env-failed':''}"><span class="session-dot ${busy?'running':build.phase==='ready'?'idle':'failed'}"></span><div><strong>${esc(busy?'Building · '+build.phase:build.phase==='ready'?'Build passed':'Build needs attention')}</strong><small>${esc(build.error||('Recipe v'+build.revision+(build.commit_sha?' · '+build.commit_sha.slice(0,8):'')))}</small></div><button class="quiet small" data-build-log="${esc(build.id)}">View log</button></div>`:'<p class="subtext">Builds when a session first uses this repository. You can also build it ahead of time.</p>'}
    ${item.active_build&&(build?.id!==item.active_build||build?.revision!==item.revision)?'<p class="environment-fallback">Sessions continue using the last successful build.</p>':''}
    ${item.activate_on_ready?'<p class="environment-fallback">After checks pass, new sessions for this repository use its prepared environment automatically.</p>':''}
    <label class="environment-refresh"><input type="checkbox" data-refresh-environment="${esc(item.id)}" ${item.refresh_daily?'checked':''} ${item.active_build?'':'disabled'}> Refresh daily and after recipe edits</label><div class="environment-card-actions"><button class="small" data-edit-environment="${esc(item.id)}">Edit recipe</button><button class="small" data-rebuild-environment="${esc(item.id)}" ${busy?'disabled':''}>${item.active_build?'Rebuild':'Build environment'}</button>${busy?`<button class="quiet small" data-cancel-build="${esc(build.id)}">Cancel build</button>`:''}${item.active_build||item.activate_on_ready?`<button class="quiet small" data-toggle-environment="${esc(item.id)}">${item.activate_on_ready?'Disable automatic setup':item.enabled?'Disable':'Enable'}</button>${item.active_build&&!item.is_default?`<button class="quiet small" data-default-environment="${esc(item.id)}">Use by default</button>`:''}`:''}</div></article>`;
}
async function renderEnvironments() {
  clearTimeout(environmentRefresh);
  const version=state.pageVersion;
  if(state.role!=='admin') {$('#content').innerHTML='<div class="page-heading"><h1>Project environments</h1><p>Ask an administrator to configure your project environments.</p></div>';return;}
  const data=await api('/api/admin/environments');
  if(version!==state.pageVersion||state.view!=='environments')return;
  $('#content').innerHTML=`<section class="environments-page"><div class="page-heading"><div><div class="eyebrow">ORGANIZATION SETTINGS</div><h1>Project environments</h1><p class="subtext">Start with your repository, dependencies, and services ready.</p></div><button class="primary" id="new-environment">＋ New environment</button></div>
    <div class="environment-explainer"><div><span>01</span><strong>${data.automatic_setup?'Discover repositories':'Configure'}</strong><p>${data.automatic_setup?'Connected GitHub repositories appear here automatically.':'Choose a template and define your setup.'}</p></div><div><span>02</span><strong>Prepare on first use</strong><p>Moyai detects setup, verifies it, and saves a snapshot.</p></div><div><span>03</span><strong>Start ready</strong><p>Sessions reuse the matching repository environment.</p></div></div>
    <div class="environment-grid">${data.environments.map(environmentCard).join('')||`<div class="environment-empty"><h2>A prepared workspace for every session</h2><p>${data.automatic_setup?'Add repositories through the GitHub connection. Each gets its own environment, built when first used.':'Use the LiteLLM template to install dependencies, prepare Postgres, and verify the real proxy.'}</p>${data.automatic_setup?'<a href="#connections">Open GitHub connection</a>':'<button class="primary" id="start-litellm">Set up LiteLLM</button>'}</div>`}</div>
    <p class="environment-footnote">Sessions without a repository use base tools unless you choose a workspace default. Existing sessions keep their saved work. Building uses Modal compute.</p></section>`;
  $('#new-environment').onclick=()=>editEnvironment(data);
  $('#start-litellm')?.addEventListener('click',()=>editEnvironment(data));
  document.querySelectorAll('[data-edit-environment]').forEach(b=>b.onclick=()=>editEnvironment(data,data.environments.find(x=>x.id===b.dataset.editEnvironment)));
  document.querySelectorAll('[data-build-log]').forEach(b=>b.onclick=()=>showEnvironmentLog(b.dataset.buildLog));
  const action=(selector,callback)=>document.querySelectorAll(selector).forEach(b=>b.onclick=async()=>{b.disabled=true;try{await callback(b);await renderEnvironments();}catch(e){toast(e.message);b.disabled=false;}});
  action('[data-rebuild-environment]',b=>{const item=data.environments.find(x=>x.id===b.dataset.rebuildEnvironment);return api(`/api/admin/environments/${item.id}/build`,{method:'POST',body:JSON.stringify({revision:item.revision})});});
  action('[data-cancel-build]',b=>api(`/api/admin/environment-builds/${b.dataset.cancelBuild}/cancel`,{method:'POST'}));
  action('[data-toggle-environment]',b=>{const item=data.environments.find(x=>x.id===b.dataset.toggleEnvironment);return api(`/api/admin/environments/${item.id}/policy`,{method:'PUT',body:JSON.stringify({enabled:item.activate_on_ready?false:!item.enabled,is_default:false})});});
  action('[data-default-environment]',b=>api(`/api/admin/environments/${b.dataset.defaultEnvironment}/policy`,{method:'PUT',body:JSON.stringify({enabled:true,is_default:true})}));
  document.querySelectorAll('[data-refresh-environment]').forEach(b=>b.onchange=async()=>{const item=data.environments.find(x=>x.id===b.dataset.refreshEnvironment);b.disabled=true;try{await api(`/api/admin/environments/${item.id}/policy`,{method:'PUT',body:JSON.stringify({enabled:!!item.enabled,is_default:!!item.is_default,refresh_daily:b.checked})});await renderEnvironments();}catch(e){b.checked=!!item.refresh_daily;b.disabled=false;toast(e.message);}});
  if(data.environments.some(e=>e.builds.some(b=>!['ready','failed'].includes(b.phase))))environmentRefresh=setTimeout(()=>{if(state.view==='environments'&&!document.querySelector('.environment-dialog'))renderEnvironments().catch(showError);},4000);
}
function editEnvironment(data,item) {
  clearTimeout(environmentRefresh);
  const dialog=document.createElement('dialog');dialog.className='environment-dialog';
  dialog.innerHTML=`<form><div class="environment-dialog-header"><div><h2>${item?'Edit recipe':'New project environment'}</h2><p>Shared with your organization after a successful build.</p></div><button type="button" class="icon-button" data-close aria-label="Close">×</button></div>
    ${!item?`<div class="field"><label for="env-template">Start from a template</label><select id="env-template">${data.templates.map((t,i)=>`<option value="${i}">${esc(t.name)}</option>`).join('')}</select></div>`:''}
    <div class="environment-form-grid"><div class="field"><label for="env-name">Name</label><input id="env-name" required maxlength="80"></div><div class="field"><label for="env-repository">GitHub repository</label><input id="env-repository" required placeholder="owner/repository"></div><div class="field"><label for="env-ref">Branch, tag, or commit</label><input id="env-ref" required></div><div class="field"><label for="env-access">Repository access</label><select id="env-access"><option value="public">Public repository</option><option value="github">Shared GitHub connection</option></select></div></div>
    <div class="field"><label for="env-mode">Setup method</label><select id="env-mode"><option value="detect">Detect repository configuration and dependencies</option><option value="manual">Custom commands</option></select><small>Detection reads .moyai/environment.json or Python, npm, and pnpm manifests. Custom containers and services need explicit commands.</small></div><div class="field"><label for="env-packages">System packages</label><input id="env-packages" placeholder="postgresql curl"><small>Debian package names, separated by spaces.</small></div>
    ${[['setup','Install dependencies','Runs once during the build.'],['startup','Start services','Runs during validation and when a session resumes. Make it safe to run again.'],['verify','Verify the environment','Must pass before this build becomes available.'],['shutdown','Stop services before saving','Stop databases cleanly before the filesystem snapshot.'],['instructions','Project instructions','Build, test, and run commands available to the agent.']].map(([id,label,help])=>`<div class="field"><label for="env-${id}">${label}</label><small>${help}</small><textarea id="env-${id}" rows="${id==='setup'||id==='instructions'?5:3}" spellcheck="false" ${id==='verify'?'required':''}></textarea></div>`).join('')}
    <p class="environment-form-note">Setup commands run in an isolated build sandbox. Use development data and keep credentials in Connections or Secrets.</p><p class="error-banner" id="env-form-error" hidden></p><div class="environment-dialog-actions"><button type="button" data-close>Cancel</button><button type="submit" class="primary">Save recipe</button></div></form>`;
  document.body.append(dialog);
  const setMode=()=>{for(const key of ['packages','setup','startup','verify','shutdown','instructions'])dialog.querySelector('#env-'+key).disabled=dialog.querySelector('#env-mode').value==='detect';};
  dialog.querySelector('#env-mode').onchange=setMode;
  const populate=recipe=>{for(const key of ['name','repository','ref','setup','startup','verify','shutdown','instructions'])dialog.querySelector('#env-'+key).value=recipe[key]||'';dialog.querySelector('#env-packages').value=(recipe.apt_packages||[]).join(' ');dialog.querySelector('#env-access').value=recipe.clone_access||'public';dialog.querySelector('#env-mode').value=recipe.setup_mode||'manual';setMode();};
  const templateIndex=Math.max(0,data.templates.findIndex(t=>t.setup_mode==='detect'));
  populate(item?.recipe||data.templates[templateIndex]);
  if(!item)dialog.querySelector('#env-template').value=String(templateIndex);
  dialog.querySelector('#env-template')?.addEventListener('change',e=>populate(data.templates[Number(e.target.value)]));
  dialog.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>dialog.close());
  dialog.addEventListener('close',()=>{dialog.remove();if(state.view==='environments')renderEnvironments().catch(showError);});
  dialog.querySelector('form').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;
    const recipe={};for(const key of ['name','repository','ref','setup','startup','verify','shutdown','instructions'])recipe[key]=dialog.querySelector('#env-'+key).value;
    recipe.apt_packages=dialog.querySelector('#env-packages').value.split(/\s+/).filter(Boolean);recipe.clone_access=dialog.querySelector('#env-access').value;recipe.setup_mode=dialog.querySelector('#env-mode').value;
    try{await api('/api/admin/environments'+(item?'/'+item.id:''),{method:item?'PUT':'POST',body:JSON.stringify({recipe,revision:item?.revision||0})});dialog.close();toast('Recipe saved. Build it to validate the environment.');}
    catch(error){const box=dialog.querySelector('#env-form-error');box.hidden=false;box.textContent=error.message;b.disabled=false;}
  };
  dialog.showModal();
}
async function showEnvironmentLog(id) {
  const dialog=document.createElement('dialog');dialog.className='environment-dialog environment-log';
  dialog.innerHTML='<div class="environment-dialog-header"><h2>Build log</h2><button class="icon-button" aria-label="Close">×</button></div><p role="status"></p><pre tabindex="0"></pre>';
  document.body.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();let timer;
  dialog.addEventListener('close',()=>{clearTimeout(timer);dialog.remove();if(state.view==='environments')renderEnvironments().catch(showError);});dialog.showModal();
  const refresh=async()=>{try{const build=await api('/api/admin/environment-builds/'+id);if(!dialog.isConnected)return;dialog.querySelector('p').textContent=`${build.phase} · recipe v${build.revision}${build.commit_sha?' · '+build.commit_sha.slice(0,8):''}${build.error?' · '+build.error:''}`;dialog.querySelector('pre').textContent=build.log||'Waiting for build output…';if(!['ready','failed'].includes(build.phase))timer=setTimeout(refresh,3000);}catch(e){if(dialog.isConnected)dialog.querySelector('p').textContent=e.message;}};await refresh();
}
