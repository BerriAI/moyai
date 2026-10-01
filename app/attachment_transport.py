"""Session-bound envelopes for reference files crossing the public web edge.

Code inside an attachment is data, but edge firewalls can mistake raw examples
for attacks. Authenticate the browser before opening this envelope, then apply
the same file inspection, ownership and storage limits as ordinary uploads.
"""
import hashlib
import struct
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CONTENT_TYPE = 'application/vnd.moyai.attachment-v1'
OVERHEAD = 8 + 12 + 16  # Timestamp, random nonce, GCM authentication tag.


def unseal_file(csrf, attachment_id, name, packet, max_file):
    if not OVERHEAD < len(packet) <= max_file + OVERHEAD:
        raise ValueError('Invalid attachment size. Add the file again.')
    stamp = struct.unpack('>Q', packet[:8])[0]
    if abs(time.time() - stamp) > 300:
        raise ValueError('The upload expired. Retry this file and check your device clock.')
    key = hashlib.sha256(b'moyai-attachment-v1\0' + csrf.encode()).digest()
    target = f'{CONTENT_TYPE}\0{attachment_id}\0{name}\0{stamp}'.encode()
    try:
        return AESGCM(key).decrypt(packet[8:20], packet[20:], target)
    except InvalidTag:
        raise ValueError('The upload could not be verified. Retry this file.') from None
