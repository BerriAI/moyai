/* Folder controls use the same authenticated API as the session sidebar. */
const sessionFolderIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/><path d="M3 9h18"/></svg>';

function restoreSessionFolderView(){
  state.folderStorageKey='moyai-folders:'+(state.userId||'shared:local:admin');
  try{state.closedFolders=new Set(JSON.parse(localStorage.getItem(state.folderStorageKey)||'[]'));}
  catch{state.closedFolders=new Set();}
}
function toggleSessionFolder(id){
  if(state.closedFolders.has(id))state.closedFolders.delete(id);else state.closedFolders.add(id);
  saveSessionFolderView();
  renderSidebar();
}
function saveSessionFolderView(){
  try{localStorage.setItem(state.folderStorageKey,JSON.stringify([...state.closedFolders]));}catch{}
}
async function assignSessionFolder(run,folderId){
  await api('/api/runs/'+run.id+'/folder',{method:'PUT',body:JSON.stringify({folder_id:folderId||null})});
  state.closedFolders.delete(folderId);
  saveSessionFolderView();
}
function bindSessionFolderDragDrop(list){
  let source=null,hovered=null;
  const mime='application/x-moyai-session';
  function highlight(target){
    if(target===hovered)return;
    hovered?.classList.remove('folder-drop-target');
    hovered=target;
    hovered?.classList.add('folder-drop-target');
  }
  function destination(event){
    if(!state.draggedSessionId||!event.dataTransfer?.types.includes(mime))return null;
    const target=event.target.closest('[data-drop-folder]');
    if(!target||!list.contains(target))return null;
    const run=state.runs.find(run=>run.id===state.draggedSessionId),folderId=target.dataset.dropFolder||null;
    if(!run||(run.folder_id||null)===folderId)return null;
    if(folderId&&!state.folders.some(folder=>folder.id===folderId))return null;
    return {target,run,folderId};
  }
  function finish(){
    const wasDragging=!!state.draggedSessionId;
    state.draggedSessionId=null;
    highlight(null);
    source?.classList.remove('session-dragging');source=null;
    list.classList.remove('session-list-dragging');
    if(wasDragging)renderSidebar();
  }
  list.addEventListener('dragstart',event=>{
    const row=event.target.closest('[data-drag-session]');
    const run=state.runs.find(run=>run.id===row?.dataset.dragSession);
    if(!run||state.folderMovePending||!event.dataTransfer){event.preventDefault();return;}
    event.dataTransfer.setData(mime,run.id);
    event.dataTransfer.effectAllowed='move';
    state.draggedSessionId=run.id;source=row;
    row.classList.add('session-dragging');list.classList.add('session-list-dragging');
  });
  const over=event=>{
    const drop=destination(event);
    highlight(drop?.target||null);
    if(drop){event.preventDefault();event.dataTransfer.dropEffect='move';}
    else if(state.draggedSessionId&&event.dataTransfer)event.dataTransfer.dropEffect='none';
  };
  list.addEventListener('dragenter',over);
  list.addEventListener('dragover',over);
  list.addEventListener('dragleave',event=>{if(!list.contains(event.relatedTarget))highlight(null);});
  list.addEventListener('dragend',finish);
  list.addEventListener('drop',async event=>{
    const drop=destination(event);
    if(!drop){finish();return;}
    event.preventDefault();event.stopPropagation();
    const folderName=state.folders.find(folder=>folder.id===drop.folderId)?.name;
    state.folderMovePending=true;finish();list.setAttribute('aria-busy','true');
    try{
      await assignSessionFolder(drop.run,drop.folderId);
      await refreshRuns();
      toast(folderName?'Moved to '+folderName+'.':'Session removed from folder.');
    }catch(error){showError(error);}
    finally{state.folderMovePending=false;list.removeAttribute('aria-busy');}
  });
}
function sessionFolderDialog(title,body){
  const dialog=$('#session-folder-dialog');
  if(dialog.open)dialog.close();
  dialog.oncancel=null;
  dialog.onclose=null;
  // Let Escape close only this dialog, keeping the mobile sidebar open.
  dialog.onkeydown=e=>{if(e.key==='Escape')e.stopPropagation();};
  dialog.innerHTML=`<form class="folder-form"><button type="button" class="dialog-close" aria-label="Close folder dialog">×</button><h2 id="session-folder-title">${esc(title)}</h2>${body}<p class="folder-error" role="alert"></p></form>`;
  dialog.querySelector('.dialog-close').onclick=()=>dialog.close();
  dialog.querySelector('[data-folder-cancel]')?.addEventListener('click',()=>dialog.close());
  dialog.showModal();
  return dialog;
}
async function saveFolderChange(dialog,action,message){
  const buttons=[...dialog.querySelectorAll('button')];
  buttons.forEach(button=>button.disabled=true);
  dialog.oncancel=e=>e.preventDefault();
  dialog.querySelector('.folder-error').textContent='';
  try{await action();}
  catch(error){dialog.querySelector('.folder-error').textContent=error.message;buttons.forEach(button=>button.disabled=false);return;}
  finally{dialog.oncancel=null;}
  dialog.close();
  try{await refreshRuns();toast(message);}catch(error){showError(error);}
  $('#new-folder')?.focus();
}
function editSessionFolder(folder=null){
  const dialog=sessionFolderDialog(folder?'Rename folder':'New folder',`
    <p class="subtext">Folders organize your sidebar. Sessions keep their existing sharing.</p>
    <div class="field"><label for="session-folder-name">Folder name</label><input id="session-folder-name" name="name" value="${esc(folder?.name||'')}" placeholder="e.g. Today, Bugs, Research" maxlength="80" required autocomplete="off"></div>
    <div class="folder-dialog-actions">${folder?'<button type="button" class="quiet folder-remove" data-folder-remove>Remove folder</button>':''}<button type="button" class="quiet" data-folder-cancel>Cancel</button><button type="submit" class="primary">${folder?'Save name':'Create folder'}</button></div>`);
  dialog.querySelector('form').onsubmit=e=>{
    e.preventDefault();const name=dialog.querySelector('input').value.trim();
    if(!name){dialog.querySelector('.folder-error').textContent='Enter a folder name.';return;}
    saveFolderChange(dialog,()=>api('/api/session-folders'+(folder?'/'+folder.id:''),{
      method:folder?'PATCH':'POST',body:JSON.stringify({name,...(folder?{revision:folder.revision}:{})})
    }),folder?'Folder renamed.':'Folder created.');
  };
  dialog.querySelector('[data-folder-remove]')?.addEventListener('click',()=>removeSessionFolder(folder));
  dialog.querySelector('input').focus();dialog.querySelector('input').select();
}
function removeSessionFolder(folder){
  const dialog=sessionFolderDialog('Remove folder?',`
    <p class="subtext">Remove <strong>${esc(folder.name)}</strong> from your sidebar? Its sessions stay saved. No conversations will be deleted.</p>
    <div class="folder-dialog-actions"><button type="button" class="quiet" data-folder-cancel>Cancel</button><button type="submit" class="primary">Remove folder</button></div>`);
  dialog.querySelector('form').onsubmit=e=>{
    e.preventDefault();saveFolderChange(dialog,()=>api('/api/session-folders/'+folder.id,{
      method:'DELETE',body:JSON.stringify({revision:folder.revision})
    }),'Folder removed. Sessions are still saved.');
  };
}
function moveSessionToFolder(run){
  const choices=[{id:'',name:'No folder'},...state.folders];
  const dialog=sessionFolderDialog('Move session',`
    <p class="folder-session-name">${esc(sessionTitle(run))}</p>
    <div class="folder-choices">${choices.map(folder=>`<button type="button" class="folder-choice" data-folder-choice="${esc(folder.id)}" aria-pressed="${(run.folder_id||'')===folder.id}">${sessionFolderIcon}<span>${esc(folder.name)}</span>${(run.folder_id||'')===folder.id?'<span aria-hidden="true">✓</span>':''}</button>`).join('')}</div>
    <div class="folder-dialog-actions"><button type="button" class="quiet" data-folder-create>New folder</button><button type="button" class="quiet" data-folder-cancel>Cancel</button></div>`);
  dialog.querySelectorAll('[data-folder-choice]').forEach(button=>button.onclick=()=>{
    const folderId=button.dataset.folderChoice;
    if(folderId===(run.folder_id||'')){dialog.close();return;}
    saveFolderChange(dialog,()=>assignSessionFolder(run,folderId),folderId?'Session moved.':'Session removed from folder.');
  });
  dialog.querySelector('[data-folder-create]').onclick=()=>{
    const create=sessionFolderDialog('New folder',`
      <p class="subtext">Create a folder and move this session into it.</p>
      <div class="field"><label for="session-folder-name">Folder name</label><input id="session-folder-name" maxlength="80" required autocomplete="off" placeholder="e.g. Today"></div>
      <div class="folder-dialog-actions"><button type="button" class="quiet" data-folder-cancel>Cancel</button><button type="submit" class="primary">Create and move</button></div>`);
    let created=null;
    create.querySelector('form').onsubmit=e=>{
      e.preventDefault();const name=create.querySelector('input').value.trim();
      if(!name){create.querySelector('.folder-error').textContent='Enter a folder name.';return;}
      saveFolderChange(create,async()=>{
        // Retain the new folder if moving fails, so a retry does not create another.
        if(!created){created=await api('/api/session-folders',{method:'POST',body:JSON.stringify({name})});create.querySelector('input').disabled=true;}
        await assignSessionFolder(run,created.id);
      },'Folder created and session moved.');
    };
    create.querySelector('input').focus();
  };
}

function showSessionActions(run,button){
  const menu=$('#session-actions');
  if(menu.matches(':popover-open'))menu.hidePopover();
  menu.innerHTML=(typeof renameSession==='function'?'<button type="button" data-rename-session>Rename</button>':'')+'<button type="button" data-move-to-folder>Move to folder</button>';
  menu.querySelector('[data-rename-session]')?.addEventListener('click',()=>{menu.hidePopover();renameSession(run);});
  menu.querySelector('[data-move-to-folder]').onclick=()=>{menu.hidePopover();moveSessionToFolder(run);};
  if(!run.parent_run_id){
    menu.insertAdjacentHTML('afterbegin',`<button type="button" data-pin-session ${state.sessionMutation?'disabled':''}>${globalThis.MoyaiIcon?.('pin',16)||''}${run.pinned?'Unpin session':'Pin session'}</button>`);
    menu.querySelector('[data-pin-session]').onclick=()=>{menu.hidePopover();changeSessionPin(run).catch(showError);};
  }
  if(typeof bindSessionLifecycleActions==='function')bindSessionLifecycleActions(menu,run);
  menu.onkeydown=e=>{
    if(e.key==='Escape'){e.stopPropagation();menu.hidePopover();button.focus();}
    if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){
      e.preventDefault();const items=[...menu.querySelectorAll('button')],index=items.indexOf(document.activeElement);
      items[e.key==='Home'?0:e.key==='End'?items.length-1:(index+(e.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
    }
  };
  const rect=button.getBoundingClientRect();
  const row=button.closest('.parent-session')?.getBoundingClientRect();
  menu.showPopover({source:button});
  const beside=row&&row.right+8+menu.offsetWidth<=innerWidth-8;
  const left=beside?row.right+8:rect.right-menu.offsetWidth;
  const top=beside?row.top:rect.bottom+4;
  menu.style.left=Math.max(8,Math.min(left,innerWidth-menu.offsetWidth-8))+'px';
  menu.style.top=Math.max(8,Math.min(top,innerHeight-menu.offsetHeight-8))+'px';
  menu.querySelector('button').focus();
}

function renameSession(run){
  const dialog=sessionFolderDialog('Rename session',`
    <div class="field"><label for="session-title-name">Session name</label><input id="session-title-name" name="title" value="${esc(sessionTitle(run))}" maxlength="80" required autocomplete="off"></div>
    <div class="folder-dialog-actions"><button type="button" class="quiet" data-folder-cancel>Cancel</button><button type="submit" class="primary">Save</button></div>`);
  const input=dialog.querySelector('input'),form=dialog.querySelector('form'),error=dialog.querySelector('.folder-error');
  const expectedTitle=run.display_title||'';
  let saving=false;
  dialog.onclose=()=>$('#session-list').querySelector(`[data-session-actions="${CSS.escape(run.id)}"]`)?.focus();
  form.onsubmit=async e=>{
    e.preventDefault();if(saving)return;
    const title=input.value.trim();
    if(!title){error.textContent='Enter a session name.';input.focus();return;}
    saving=true;error.textContent='';
    const controls=[...form.querySelectorAll('button,input')];controls.forEach(control=>control.disabled=true);
    dialog.oncancel=event=>event.preventDefault();
    try{
      const saved=await api('/api/runs/'+run.id+'/title',{method:'PUT',body:JSON.stringify({title,expected_title:expectedTitle})});
      // Invalidate reads started before this save, then update all visible title consumers.
      ++state.runsRefresh;state.chatRefresh=(state.chatRefresh||0)+1;
      state.titleEdits=(state.titleEdits||0)+1;
      if(state.chatRun?.id===saved.id)state.chatRun.display_title=saved.display_title;
      syncRunSummary(saved);
      dialog.close();toast('Session renamed.');
    }catch(failure){
      error.textContent=failure.message;
      // Reopening after a conflict must start with the server's current title.
      await refreshRuns().catch(()=>{});
    }
    finally{saving=false;controls.forEach(control=>control.disabled=false);dialog.oncancel=null;}
  };
  input.focus();input.select();
}

async function changeSessionPin(run){
  if(state.sessionMutation)return;
  state.sessionMutation=run.id;
  const actor=state.userId,pinned=!run.pinned;
  try{
    await api('/api/runs/'+run.id+'/pin',{method:'PUT',body:JSON.stringify({pinned})});
    if(actor!==state.userId)return;
    // A list/detail read started before this write must not undo its result.
    state.runsRefresh++;
    state.chatRefresh=(state.chatRefresh||0)+1;
    state.sessionEdits=(state.sessionEdits||0)+1;
    for(const item of [run,...state.runs,state.chatRun,state.sessionHeaderRun]){
      if(item?.id===run.id)item.pinned=pinned;
    }
    if(pinned){state.closedFolders.delete('pinned');saveSessionFolderView();}
    renderSidebar();
    toast(pinned?'Session pinned for you.':'Session unpinned.');
    await refreshRuns();
    $('#session-list').querySelector(`[data-session-actions="${CSS.escape(run.id)}"]`)?.focus();
  }finally{state.sessionMutation=null;}
}
