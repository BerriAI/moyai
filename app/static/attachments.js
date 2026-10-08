const attachmentDrafts = new Map();
const attachmentLimits = {count:8, file:10*1024*1024, total:20*1024*1024};
const imageFile = file => /^image\/(png|jpeg|webp|gif)$/.test(file.type || file.media_type || '');
const audioFile = file => /^(audio\/|video\/(webm|mp4))/.test(file.media_type || file.type || '') || /\.(mp3|mpga|mpeg|wav|m4a|mp4|webm|ogg|oga|flac)$/i.test(file.name);
function fileSize(size){return size<1024?`${size} B`:size<1024*1024?`${Math.ceil(size/1024)} KB`:`${(size/1024/1024).toFixed(1)} MB`;}
function transferFiles(transfer){
  const items=Array.from(transfer?.items||[]).filter(item=>item.kind==='file').map(item=>item.getAsFile()).filter(Boolean);
  return items.length?items:Array.from(transfer?.files||[]);
}
function attachmentError(files,file){
  if(!file.size)return `${file.name} is empty.`;
  if(file.size>attachmentLimits.file)return `${file.name} is larger than 10 MB.`;
  if(files.length>=attachmentLimits.count)return `Attach up to ${attachmentLimits.count} files per message.`;
  if(files.reduce((size,item)=>size+item.size,0)+file.size>attachmentLimits.total)return 'Attachments must total 20 MB or less.';
  return '';
}
function fileVisual(file,src=''){
  return src && imageFile(file)?`<img src="${esc(src)}" alt="${esc(file.name)}" loading="lazy">`:`<span class="file-type" aria-hidden="true">${esc(file.name.split('.').pop().slice(0,6).toUpperCase()||'FILE')}</span>`;
}
function showAttachment(file,useTranscript=null){
  const dialog=MoyaiUI.createDialog();dialog.className='attachment-preview';
  MoyaiUI.render(dialog, `<div class="attachment-preview-heading"><div><strong>${esc(file.name)}</strong><small>${fileSize(file.size)}</small></div><button class="icon-button" aria-label="Close preview">×</button></div>${audioFile(file)?`<div class="audio-preview"><audio controls preload="metadata" src="${esc(file.audio_url||file.localUrl||'')}"></audio><h3>Transcript</h3><p class="audio-transcript">${esc(file.transcript||'The transcript will appear after this recording finishes uploading.')}</p>${useTranscript&&file.transcript?'<button type="button" class="use-transcript">Edit in message</button><p class="subtext">Review names and instructions before sending.</p>':''}</div>`:file.preview_url||file.localUrl?`<img class="attachment-full-image" src="${esc(file.preview_url||file.localUrl)}" alt="${esc(file.name)}">`:file.preview_text?`<pre>${esc(file.preview_text)}</pre><p class="subtext">Text preview · the full file is available to Moyai.</p>`:'<div class="attachment-no-preview">'+fileVisual(file)+'<p>This file will be available in your session.</p></div>'}${file.url?`<a class="attachment-download" href="${esc(file.url)}" download>Download original ↗</a>`:''}`);
  document.body.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();
  const use=dialog.querySelector('.use-transcript');if(use)use.onclick=()=>{if(useTranscript(file.transcript))dialog.close();};
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
  let depth=0,recorder=null,active=true;
  const region=document.createElement('div');region.className='attachment-tray';region.setAttribute('aria-label','Attachments');
  form.prepend(region);
  const picker=MoyaiUI.createElement('input', document);picker.type='file';picker.multiple=true;picker.hidden=true;picker.setAttribute('aria-label','Choose attachments');form.append(picker);
  const button=MoyaiUI.createElement('button', document);button.type='button';button.className='quiet attach-button';button.title='Attach files · or paste an image';button.setAttribute('aria-label','Attach files');
  MoyaiUI.render(button, `${globalThis.MoyaiIcon?.('paperclip',16)||''}<span>Attach</span>`);
  form.querySelector('.composer-toolbar').prepend(button);
  const announce=document.createElement('span');announce.className='sr-only';announce.setAttribute('role','status');form.append(announce);
  const notify=()=>draft.listeners.forEach(listener=>listener());
  const render=()=>{
    region.hidden=!draft.items.length;button.disabled=!!draft.locked;
    input.required=!draft.items.length;recorder?.render();
    MoyaiUI.render(region, draft.items.map(file=>`<div class="draft-attachment ${imageFile(file)?'is-image':''} ${file.status==='error'?'upload-error':''}"><button type="button" class="attachment-open" data-preview="${file.id}" aria-label="Preview ${esc(file.name)}">${fileVisual(file,file.preview_url||file.localUrl)}<span><strong>${esc(file.name)}</strong><small>${file.status==='uploading'?(audioFile(file)?'Uploading & transcribing…':'Uploading…'):file.status==='error'?'Upload failed':file.transcript?'Transcript ready · '+fileSize(file.size):fileSize(file.size)}</small></span></button><button type="button" class="attachment-remove" data-remove="${file.id}" aria-label="Remove ${esc(file.name)}" ${draft.locked?'disabled':''}>×</button>${file.status==='error'?`<p class="attachment-error-detail">${esc(file.error)}</p><button type="button" class="attachment-retry" data-retry="${file.id}" ${draft.locked?'disabled':''}>Retry</button>`:''}</div>`).join(''));
  };
  function insertTranscript(text){
    if(!active||draft.locked)return false;
    const addition=(input.value.trim()?'\n\n':'')+text;
    if(input.value.length+addition.length>16000){toast('The transcript is too long to add to this message. Shorten the message first.');return false;}
    input.value+=addition;input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();return true;
  }
  draft.listeners.add(render);render();
  const discard=async item=>{try{await api('/api/attachments/'+item.id,{method:'DELETE'});}catch{/* Unsent uploads expire after 24 hours. */}};
  async function upload(item){
    item.status='uploading';item.abort=new AbortController();notify();
    try{
      const body=await sealAttachment(item.file,item.id,item.name,state.csrf);
      if(!draft.items.includes(item))return;
      const saved=await api('/api/attachments/'+item.id+'?name='+encodeURIComponent(item.name),{method:'PUT',body,headers:{'Content-Type':attachmentContentType},signal:item.abort.signal});
      if(!draft.items.includes(item)){await discard(item);return;}
      Object.assign(item,saved,{status:'ready'});announce.textContent=item.name+' attached';
      if(item.dictate&&saved.transcript)insertTranscript(saved.transcript);
    }catch(error){if(draft.items.includes(item)){item.status='error';item.error=error.message;toast(error.message);}}
    finally{notify();}
  }
  async function add(files,dictate=false){
    if(draft.locked)return;
    const pending=[];
    for(const file of files){
      const error=attachmentError(draft.items,file);if(error){toast(error);continue;}
      const item={id:crypto.randomUUID().replaceAll('-',''),file,dictate,name:file.name||'Screenshot.png',size:file.size,type:file.type,status:'uploading',localUrl:imageFile(file)||audioFile(file)?URL.createObjectURL(file):''};
      draft.items.push(item);pending.push(item);
    }
    notify();input.focus();
    for(const item of pending)if(draft.items.includes(item))await upload(item);
  }
  recorder=typeof bindAudioRecorder==='function'?bindAudioRecorder(form,file=>add([file],true),()=>!!draft.locked):null;
  button.onclick=()=>picker.click();picker.onchange=()=>{add(Array.from(picker.files));picker.value='';};
  region.onclick=event=>{
    const target=event.target.closest('button');if(!target)return;
    const id=target.dataset.preview||target.dataset.remove||target.dataset.retry,item=draft.items.find(file=>file.id===id);if(!item)return;
    if(target.dataset.preview){showAttachment(item,insertTranscript);return;}
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
    hasFiles(){return draft.items.length>0;},
    ids(){if(recorder?.busy())throw Error('Stop or cancel the recording before sending.');if(draft.items.some(item=>item.status!=='ready'))throw Error('Wait for uploads to finish, or retry/remove the failed attachment.');return draft.items.map(item=>item.id);},
    lock(value){draft.locked=value;notify();},
    clear(ids){for(const item of draft.items.filter(file=>ids.includes(file.id))){if(item.localUrl)URL.revokeObjectURL(item.localUrl);}draft.items=draft.items.filter(file=>!ids.includes(file.id));notify();},
    destroy(){active=false;recorder?.destroy();draft.listeners.delete(render);document.removeEventListener('dragover',outside);document.removeEventListener('drop',outside);},
  };
}
