/* Controller-owned containers retain independent shadcn component regions. */
(function(root){
  const records=new WeakMap();
  function sync(host,html){
    const focused=host.ownerDocument.activeElement;
    const template=host.ownerDocument.createElement('template');template.innerHTML=html;
    patch(host,[...template.content.children]);
    // insertBefore can blur a moved control on browsers without moveBefore.
    if(focused&&host.contains(focused)&&host.ownerDocument.activeElement!==focused)focused.focus({preventScroll:true});
  }
  function remove(host,node){
    if(node.parentNode!==host)return;
    // Dispose synchronously, including roots mounted in a detached container
    // that the shared removal observer has not yet seen in the document.
    if(node.nodeType===1)MoyaiUI.render(node,'');
    node.remove();
  }
  function patch(host,sources){
    if(!sources.length){
      if(host.childNodes.length)MoyaiUI.render(host,'');
      records.delete(host);return;
    }
    const previous=new Map(records.get(host)),next=new Map();
    sources.forEach((source,index)=>{
      const explicit=source.getAttribute('data-region-key'),key=explicit===null?'position:'+index:'key:'+explicit;
      const mode=explicit===null||source.hasAttribute('data-region-leaf')?'leaf':source.hasAttribute('data-region-preserve')?'preserve':'container';
      let entry=previous.get(key);
      if(entry&&(entry.node.parentNode!==host||entry.tag!==source.tagName||entry.mode!==mode)){
        remove(host,entry.node);entry=null;
      }
      if(!entry){
        const node=host.ownerDocument.createElement(explicit===null?'moyai-region':source.tagName);
        if(explicit===null)node.style.display='contents';
        entry={node,tag:source.tagName,mode,html:null,attributes:new Set()};
      }
      const {node}=entry;
      if(explicit!==null){
        const attributes=new Set([...source.attributes].map(attr=>attr.name));
        for(const name of entry.attributes)if(!attributes.has(name))node.removeAttribute(name);
        for(const attr of source.attributes)if(node.getAttribute(attr.name)!==attr.value)node.setAttribute(attr.name,attr.value);
        entry.attributes=attributes;
      }
      // Attach before mounting so React records the root's connected state.
      if(host.children[index]!==node){
        const before=host.children[index]||null;
        if(node.parentNode===host&&host.moveBefore)host.moveBefore(node,before);
        else host.insertBefore(node,before);
      }
      if(mode==='leaf'){
        const markup=explicit===null?source.outerHTML:source.innerHTML;
        if(entry.html!==markup){MoyaiUI.render(node,markup);entry.html=markup;}
      }else if(mode==='container')patch(node,[...source.children]);
      next.set(key,entry);previous.delete(key);
    });
    previous.forEach(({node})=>remove(host,node));
    // A first mount can replace a loading region without leaving stale nodes.
    const retained=new Set([...next.values()].map(entry=>entry.node));
    for(const child of [...host.childNodes])if(!retained.has(child))remove(host,child);
    records.set(host,next);
  }
  root.MoyaiRegions={sync};
})(globalThis);
