/* Resolve model-written references only against the authenticated saved archive. */
(function(root,factory){
  const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.MoyaiFiles=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function reference(value){
    let path=String(value||'').trim();
    try{path=decodeURIComponent(path);}catch{return null;}
    path=path.replace(/^sandbox:(?=\/workspace\/)/,'').replace(/^\/workspace\//,'').replace(/^\.\//,'');
    if(!path||path.startsWith('/')||/[\\:#?\x00-\x1f\x7f]/.test(path)||path.split('/').some(p=>!p||p==='.'||p==='..'))return null;
    return path;
  }
  function resolve(value,files){
    const path=reference(value);if(!path)return null;
    const exact=files.filter(file=>file.workspace_path===path);
    if(exact.length===1)return exact[0];
    // A bare filename may be shorthand for a nested file, but never guess
    // between two repositories or reinterpret an explicit missing path.
    if(path.includes('/'))return null;
    const matches=files.filter(file=>file.workspace_path&&file.name===path);
    return matches.length===1?matches[0]:null;
  }
  function create({api,markdown,escape:esc,size}){
    let run=null,catalog=null,key='',loading=null,requestId=0,previewId=0,viewId=0,selected=null,dialog=null;
    const doc=document;
    const query=selector=>doc.querySelector(selector);
    function decorate(container){
      if(!container||!catalog||!run)return;
      container.querySelectorAll('.markdown [data-file-ref],.markdown strong,.markdown code').forEach(node=>{
        if(node.closest('pre')||(!node.matches('[data-file-ref]')&&(node.closest('a')||node.children.length)))return;
        const ref=node.dataset.fileRef||node.textContent;
        const file=resolve(ref,catalog.files);
        if(!file){if(node.dataset.savedFile){node.removeAttribute('href');delete node.dataset.savedFile;delete node.dataset.fileRun;node.classList.remove('saved-file-link');}return;}
        let link=node;
        if(node.tagName!=='A'){link=doc.createElement('a');link.textContent=node.textContent;node.replaceChildren(link);}
        link.href=file.url;link.classList.add('saved-file-link');link.dataset.fileRef=ref;link.dataset.savedFile=file.archive_path;link.dataset.fileRun=run.id;
        link.removeAttribute('target');link.removeAttribute('rel');link.title='Preview '+file.path;
      });
    }
    function update(){
      const button=query('#files-button');
      if(button){button.hidden=!run?.has_artifact;button.textContent='Files'+(catalog?' · '+catalog.files.length:'');button.onclick=()=>open();}
      const area=query('#artifact-area');
      if(area)area.innerHTML=run?.has_artifact?`<button type="button" class="quiet browse-files">Browse saved files${catalog?' · '+catalog.files.length:''}</button><a class="session-download" href="/api/runs/${run.id}/artifact">↓ Download workspace ZIP</a>`:'';
      if(area?.querySelector('.browse-files'))area.querySelector('.browse-files').onclick=()=>open();
      decorate(query('#conversation'));
    }
    async function load(force=false){
      if(!run?.has_artifact)return;
      const next=run.id+':'+(run.events||[]).filter(e=>e.kind==='artifact').at(-1)?.id;
      if(!force&&key===next)return loading;
      key=next;const id=++requestId,rid=run.id;
      loading=(async()=>{
        try{
          const data=await api(`/api/runs/${rid}/files`);
          if(id!==requestId)return;
          catalog=data;update();
        }catch(error){if(id!==requestId)return;catalog={files:[],error:error.message};key='';update();}
      })();
      return loading;
    }
    function sync(value){
      if(run?.id!==value.id){requestId++;previewId++;catalog=null;key='';dialog?.close();}
      run=value;update();load();
    }
    function reset(){run=null;catalog=null;key='';requestId++;previewId++;dialog?.close();}
    function drawList(){
      const filter=query('#saved-file-search').value.toLowerCase();
      const files=(catalog?.files||[]).filter(file=>file.path.toLowerCase().includes(filter));
      query('#saved-file-list').innerHTML=files.length?files.map(file=>`<button type="button" class="saved-file-choice ${file.archive_path===selected?.archive_path?'selected':''}" data-file-choice="${esc(file.archive_path)}" ${file.archive_path===selected?.archive_path?'aria-current="true"':''}><span class="saved-file-icon" aria-hidden="true">▤</span><span><strong>${esc(file.name)}</strong><small>${esc(file.path)} · ${size(file.size)}</small></span></button>`).join(''):'<p class="saved-file-empty">No matching files.</p>';
      query('#saved-file-list').querySelectorAll('[data-file-choice]').forEach(button=>button.onclick=()=>select(catalog.files.find(file=>file.archive_path===button.dataset.fileChoice)));
    }
    async function select(file){
      if(!file)return;selected=file;const id=++previewId;
      drawList();
      query('#saved-file-title').textContent=file.name;
      query('#saved-file-path').textContent=file.path;
      const download=query('#saved-file-download');download.href=file.url;download.hidden=false;download.setAttribute('download',file.name);
      query('#saved-file-preview').innerHTML='<p class="saved-file-empty" role="status">Opening file…</p>';
      try{
        const result=await api(file.preview_url);if(id!==previewId||!dialog.open)return;
        const target=query('#saved-file-preview');
        target.innerHTML=result.text===null?'<p class="saved-file-empty">Preview isn’t available for this file type. Use Download to open it.</p>':
          `${result.truncated?'<p class="saved-file-notice">Showing the first 128 KB. Use Download for the complete file.</p>':''}${result.format==='markdown'?`<div class="markdown">${markdown(result.text)}</div>`:`<pre class="saved-file-text">${esc(result.text)}</pre>`}`;
        target.scrollTop=0;decorate(target);
        target.querySelectorAll('.copy-code').forEach(button=>button.onclick=async()=>{try{await navigator.clipboard.writeText(button.closest('.code-block').querySelector('code').textContent);button.textContent='Copied';}catch{button.textContent='Select text to copy';}});
      }catch(error){if(id===previewId&&dialog.open)query('#saved-file-preview').innerHTML=`<p class="saved-file-empty" role="alert">${esc(error.message)}</p>`;}
    }
    async function open(file=null){
      if(!run?.has_artifact)return;
      if(!dialog){dialog=doc.createElement('dialog');dialog.id='saved-files-dialog';dialog.setAttribute('aria-labelledby','saved-files-heading');doc.body.append(dialog);dialog.addEventListener('close',()=>{previewId++;viewId++;});}
      const rid=run.id,viewing=++viewId;
      dialog.innerHTML=`<header class="saved-files-heading"><div><h2 id="saved-files-heading">Saved files</h2><p>Latest saved version of this workspace</p></div><button type="button" class="icon-button" aria-label="Close saved files">×</button></header><div class="saved-files-loading" role="status">Loading saved files…</div>`;
      dialog.querySelector('button').onclick=()=>dialog.close();
      if(!dialog.open)dialog.showModal();
      await load(true);if(run?.id!==rid||!dialog.open||viewing!==viewId)return;
      dialog.querySelector('.saved-files-loading').outerHTML=`<div class="saved-files-body"><aside class="saved-files-nav"><input id="saved-file-search" type="search" placeholder="Find a file…" aria-label="Find a saved file"><div id="saved-file-list"></div></aside><section class="saved-file-view"><div class="saved-file-heading"><div><h3 id="saved-file-title">Select a file</h3><p id="saved-file-path"></p></div><a id="saved-file-download" class="small" hidden>↓ Download</a></div><div id="saved-file-preview"><p class="saved-file-empty">Choose a file to preview.</p></div></section></div><footer class="saved-files-footer"><span>${esc(catalog?.error||catalog?.note||'')}${catalog?.limited?' Some entries cannot be previewed.':''}</span><a href="/api/runs/${rid}/artifact">Download ZIP</a></footer>`;
      selected=null;drawList();query('#saved-file-search').oninput=drawList;
      const choice=file?catalog.files.find(item=>item.archive_path===file.archive_path):catalog.files.find(item=>item.workspace_path)||catalog.files[0];
      if(choice)select(choice);
    }
    doc.addEventListener('click',event=>{
      const link=event.target.closest('[data-saved-file]');
      if(!link||link.dataset.fileRun!==run?.id||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||event.button)return;
      const file=catalog?.files.find(item=>item.archive_path===link.dataset.savedFile);if(!file)return;
      event.preventDefault();if(dialog?.open){if(query('#saved-file-list'))select(file);}else open(file);
    });
    return {sync,decorate,reset,open};
  }
  return {reference,resolve,create};
});
