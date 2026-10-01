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
