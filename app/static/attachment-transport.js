const attachmentContentType = 'application/vnd.moyai.attachment-v1';

async function sealAttachment(file, id, name, csrf) {
  if (!crypto.subtle || !csrf) throw Error('Refresh the page before uploading this file.');
  const encoder = new TextEncoder();
  const keyBytes = await crypto.subtle.digest('SHA-256', encoder.encode('moyai-attachment-v1\0' + csrf));
  const key = await crypto.subtle.importKey('raw', keyBytes, 'AES-GCM', false, ['encrypt']);
  const stamp = Math.floor(Date.now() / 1000);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const target = encoder.encode(`${attachmentContentType}\0${id}\0${name}\0${stamp}`);
  const raw = await file.arrayBuffer();
  const encrypted = await crypto.subtle.encrypt({name:'AES-GCM', iv:nonce, additionalData:target}, key, raw);
  const packet = new Uint8Array(20 + encrypted.byteLength);
  new DataView(packet.buffer).setBigUint64(0, BigInt(stamp));
  packet.set(nonce, 8);
  packet.set(new Uint8Array(encrypted), 20);
  return packet;
}

async function uploadAttachment(file, id, name, csrf, send, signal, progress = () => {}) {
  const base = '/api/attachments/' + id;
  const transfer = await send(base + '/upload', {method:'POST', body:JSON.stringify({name,size:file.size}),signal});
  if (!Number.isSafeInteger(transfer.part_size) || transfer.part_size <= 0 || transfer.size !== file.size) {
    throw Error('The upload service returned invalid transfer metadata. Refresh and retry.');
  }
  for (let offset=0,part=0; offset<file.size; offset+=transfer.part_size,part++) {
    signal?.throwIfAborted();
    const source=file.slice(offset,offset+transfer.part_size);
    const raw=await source.arrayBuffer();
    const checksum=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',raw)),byte=>byte.toString(16).padStart(2,'0')).join('');
    const body=await sealAttachment(new Blob([raw]),`${id}:${part}`,name,csrf);
    signal?.throwIfAborted();
    const receipt=await send(base + '/parts/' + part + '?name=' + encodeURIComponent(name),
      {method:'PUT',body,headers:{'Content-Type':attachmentContentType},signal});
    if(receipt.part!==part || receipt.sha256!==checksum)throw Error('The upload receipt did not match this file. Retry the attachment.');
    progress(Math.min(file.size,offset+source.size),file.size);
  }
  signal?.throwIfAborted();
  return send(base + '/complete',{method:'POST',signal});
}
