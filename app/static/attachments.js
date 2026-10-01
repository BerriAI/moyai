const attachmentDrafts = new Map();
const attachmentLimits = {count:5, file:10*1024*1024, total:20*1024*1024};
const imageFile = file => /^image\/(png|jpeg|webp|gif)$/.test(file.type || file.media_type || '');
function fileSize(size){return size<1024?`${size} B`:size<1024*1024?`${Math.ceil(size/1024)} KB`:`${(size/1024/1024).toFixed(1)} MB`;}
function transferFiles(transfer){
  const items=Array.from(transfer?.items||[]).filter(item=>item.kind==='file').map(item=>item.getAsFile()).filter(Boolean);
  return items.length?items:Array.from(transfer?.files||[]);
}
function attachmentError(files,file){
  if(!file.size)return `${file.name} is empty.`;
  if(file.size>attachmentLimits.file)return `${file.name} is larger than 10 MB.`;
  if(files.length>=attachmentLimits.count)return 'Attach up to 5 files per message.';
  if(files.reduce((size,item)=>size+item.size,0)+file.size>attachmentLimits.total)return 'Attachments must total 20 MB or less.';
  return '';
}
function fileVisual(file,src=''){
  return src?`<img src="${esc(src)}" alt="${esc(file.name)}" loading="lazy">`:`<span class="file-type" aria-hidden="true">${esc(file.name.split('.').pop().slice(0,6).toUpperCase()||'FILE')}</span>`;
}
function showAttachment(file){
  const dialog=document.createElement('dialog');dialog.className='attachment-preview';
  dialog.innerHTML=`<div class="attachment-preview-heading"><div><strong>${esc(file.name)}</strong><small>${fileSize(file.size)}</small></div><button class="icon-button" aria-label="Close preview">×</button></div>${file.preview_url||file.localUrl?`<img class="attachment-full-image" src="${esc(file.preview_url||file.localUrl)}" alt="${esc(file.name)}">`:file.preview_text?`<pre>${esc(file.preview_text)}</pre><p class="subtext">Text preview · the full file is available to Moyai.</p>`:'<div class="attachment-no-preview">'+fileVisual(file)+'<p>This file will be available in your session.</p></div>'}${file.url?`<a class="attachment-download" href="${esc(file.url)}" download>Download original ↗</a>`:''}`;
  document.body.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>dialog.remove());
  dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
  dialog.showModal();
}
function messageAttachments(files=[]){
  return files.length?`<div class="message-attachments">${files.map(file=>`<button type="button" class="sent-attachment ${file.preview_url?'is-image':''}" data-attachment="${esc(file.id)}" aria-label="Preview ${esc(file.name)}">${fileVisual(file,file.preview_url)}<span><strong>${esc(file.name)}</strong><small>${fileSize(file.size)}</small></span></button>`).join('')}</div>`:'';
}
function bindAttachments(input,form,key){
  if(!attachmentDrafts.has(key))attachmentDrafts.set(key,{items:[],listeners:new Set()});
  const draft=attachmentDrafts.get(key);
  let depth=0;
  const region=document.createElement('div');region.className='attachment-tray';region.setAttribute('aria-label','Attachments');
  form.prepend(region);
  const picker=document.createElement('input');picker.type='file';picker.multiple=true;picker.hidden=true;picker.setAttribute('aria-label','Choose attachments');form.append(picker);
  const button=document.createElement('button');button.type='button';button.className='quiet attach-button';button.title='Attach files · or paste an image';button.setAttribute('aria-label','Attach files');
  button.innerHTML='<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l8-8"/><path d="m6 10-1 1a3 3 0 0 0 4 4l7-7"/></svg>';
  form.querySelector('.composer-toolbar').prepend(button);
  const announce=document.createElement('span');announce.className='sr-only';announce.setAttribute('role','status');form.append(announce);
  const notify=()=>draft.listeners.forEach(listener=>listener());
  const render=()=>{
    region.hidden=!draft.items.length;button.disabled=!!draft.locked;
    input.required=!draft.items.length;
    region.innerHTML=draft.items.map(file=>`<div class="draft-attachment ${imageFile(file)?'is-image':''} ${file.status==='error'?'upload-error':''}"><button type="button" class="attachment-open" data-preview="${file.id}" aria-label="Preview ${esc(file.name)}">${fileVisual(file,file.preview_url||file.localUrl)}<span><strong>${esc(file.name)}</strong><small>${file.status==='uploading'?'Uploading…':file.status==='error'?'Upload failed':fileSize(file.size)}</small></span></button><button type="button" class="attachment-remove" data-remove="${file.id}" aria-label="Remove ${esc(file.name)}" ${draft.locked?'disabled':''}>×</button>${file.status==='error'?`<button type="button" class="attachment-retry" data-retry="${file.id}" ${draft.locked?'disabled':''}>Retry</button>`:''}</div>`).join('');
  };
  draft.listeners.add(render);render();
  const discard=async item=>{try{await api('/api/attachments/'+item.id,{method:'DELETE'});}catch{/* Unsent uploads expire after 24 hours. */}};
  async function upload(item){
    item.status='uploading';item.abort=new AbortController();notify();
    try{
      const saved=await api('/api/attachments/'+item.id+'?name='+encodeURIComponent(item.name),{method:'PUT',body:item.file,headers:{'Content-Type':'application/octet-stream'},signal:item.abort.signal});
      if(!draft.items.includes(item)){await discard(item);return;}
      Object.assign(item,saved,{status:'ready'});announce.textContent=item.name+' attached';
    }catch(error){if(draft.items.includes(item)){item.status='error';item.error=error.message;toast(error.message);}}
    finally{notify();}
  }
  async function add(files){
    if(draft.locked)return;
    const pending=[];
    for(const file of files){
      const error=attachmentError(draft.items,file);if(error){toast(error);continue;}
      const item={id:crypto.randomUUID().replaceAll('-',''),file,name:file.name||'Screenshot.png',size:file.size,type:file.type,status:'uploading',localUrl:imageFile(file)?URL.createObjectURL(file):''};
      draft.items.push(item);pending.push(item);
    }
    notify();input.focus();
    for(const item of pending)if(draft.items.includes(item))await upload(item);
  }
  button.onclick=()=>picker.click();picker.onchange=()=>{add(Array.from(picker.files));picker.value='';};
  region.onclick=event=>{
    const target=event.target.closest('button');if(!target)return;
    const id=target.dataset.preview||target.dataset.remove||target.dataset.retry,item=draft.items.find(file=>file.id===id);if(!item)return;
    if(target.dataset.preview){showAttachment(item);return;}
    if(draft.locked)return;
    if(target.dataset.retry){upload(item);return;}
    draft.items.splice(draft.items.indexOf(item),1);item.abort?.abort();if(item.localUrl)URL.revokeObjectURL(item.localUrl);discard(item);notify();input.focus();
  };
  const paste=event=>{const files=transferFiles(event.clipboardData);if(files.length){event.preventDefault();add(files);}};
  const hasFiles=event=>Array.from(event.dataTransfer?.types||[]).includes('Files');
  const enter=event=>{if(hasFiles(event)){event.preventDefault();depth++;form.classList.add('attachment-dragging');}};
  const over=event=>{if(hasFiles(event)){event.preventDefault();event.dataTransfer.dropEffect='copy';}};
  const leave=event=>{if(hasFiles(event)&&--depth<=0){depth=0;form.classList.remove('attachment-dragging');}};
  const drop=event=>{if(hasFiles(event)){event.preventDefault();event.stopPropagation();depth=0;form.classList.remove('attachment-dragging');add(transferFiles(event.dataTransfer));}};
  form.addEventListener('paste',paste);form.addEventListener('dragenter',enter);form.addEventListener('dragover',over);form.addEventListener('dragleave',leave);form.addEventListener('drop',drop);
  const outside=event=>{if(hasFiles(event)&&!form.contains(event.target))event.preventDefault();};
  document.addEventListener('dragover',outside);document.addEventListener('drop',outside);
  return {
    ids(){if(draft.items.some(item=>item.status!=='ready'))throw Error('Wait for uploads to finish, or retry/remove the failed attachment.');return draft.items.map(item=>item.id);},
    lock(value){draft.locked=value;notify();},
    clear(ids){for(const item of draft.items.filter(file=>ids.includes(file.id))){if(item.localUrl)URL.revokeObjectURL(item.localUrl);}draft.items=draft.items.filter(file=>!ids.includes(file.id));notify();},
    destroy(){draft.listeners.delete(render);document.removeEventListener('dragover',outside);document.removeEventListener('drop',outside);},
  };
}
