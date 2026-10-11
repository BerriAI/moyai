"""Complete input files in immutable transport parts, with one published manifest.

The existing attachment row reserves the declared bytes against workspace
storage. Parts remain private drafts until inspection and full-file verification
finish. Retrying an acknowledged part can never replace its contents.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import tempfile

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


PART_BYTES = 1024 * 1024


class UploadSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    size: int = Field(gt=0, strict=True)


class AttachmentUploads:
    def __init__(self, attachments):
        self.attachments = attachments
        self.store = attachments.store

    def owned(self, conn, attachment_id, owner_id):
        row = conn.execute('SELECT * FROM attachments WHERE id=?', (attachment_id,)).fetchone()
        if not row or row['owner_id'] != owner_id:
            raise HTTPException(404, 'Upload not found.')
        if not row['part_size']:
            raise HTTPException(409, 'This upload ID belongs to a different transfer. Add the file again.')
        return row

    def begin(self, attachment_id, owner_id, name, size, storage_limit):
        from .attachments import filename
        name = filename(name)
        if type(size) is not int or size <= 0:
            raise ValueError('Choose a nonempty file with a valid size.')
        stamp = datetime.now(timezone.utc)
        cutoff = (stamp-timedelta(days=1)).isoformat()
        with self.store.connect() as conn:
            conn.begin_write()
            conn.execute('DELETE FROM attachments WHERE message_id IS NULL AND created_at<?', (cutoff,))
            prior = conn.execute('SELECT * FROM attachments WHERE id=?', (attachment_id,)).fetchone()
            if prior:
                if (prior['owner_id'], prior['name'], prior['size']) != (owner_id, name, size) or not prior['part_size']:
                    raise HTTPException(409, 'This upload ID was already used. Add the file again.')
            else:
                self.attachments.check_quota(conn, owner_id, size, 0, storage_limit, cutoff)
                conn.execute('''INSERT INTO attachments(id,owner_id,name,size,sha256,media_type,preview_text,
                    created_at,data,preview,part_size,upload_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,'uploading')''',
                    (attachment_id, owner_id, name, size, '', 'application/octet-stream', '', stamp.isoformat(),
                     b'', b'', PART_BYTES))
            row = self.owned(conn, attachment_id, owner_id)
            return {'part_size':row['part_size'], 'size':row['size'], 'state':row['upload_state']}

    def part(self, attachment_id, owner_id, part, raw):
        checksum = hashlib.sha256(raw).hexdigest()
        identity = ('owner_id', 'name', 'size', 'created_at', 'part_size')
        def check(conn, original=None):
            row = self.owned(conn, attachment_id, owner_id)
            if original and any(row[key] != original[key] for key in identity):
                raise HTTPException(409, 'The file upload changed while saving a part.')
            remaining = row['size'] - part*row['part_size']
            if type(part) is not int or part < 0 or remaining <= 0 or len(raw) != min(remaining, row['part_size']):
                raise ValueError('Upload part does not match the declared file range.')
            prior = conn.execute('SELECT sha256 FROM attachment_parts WHERE attachment_id=? AND part=?',
                                 (attachment_id, part)).fetchone()
            if prior and prior['sha256'] != checksum:
                raise HTTPException(409, 'This upload part already contains different bytes. Add the file again.')
            if not prior and row['upload_state'] != 'uploading':
                raise HTTPException(409, 'This attachment is already complete.')
            return prior is not None, dict(row)
        with self.store.connect() as conn:
            exists, original = check(conn)
            if exists:
                return {'part':part, 'sha256':checksum}
        reference = self.store.objects.put(raw) if self.store.objects.enabled else ''
        with self.store.connect() as conn:
            conn.begin_write()
            if not check(conn, original)[0]:
                conn.execute('INSERT INTO attachment_parts VALUES(?,?,?,?,?,?)',
                    (attachment_id, part, len(raw), checksum, reference, b'' if reference else raw))
        return {'part':part, 'sha256':checksum}

    def chunks(self, row, offset=0, length=None):
        # Only one bounded part is materialized at a time, including local BLOBs.
        end = row['size'] if length is None else min(row['size'], offset+length)
        part = offset//row['part_size']
        position, checksum = part*row['part_size'], hashlib.sha256()
        while position < end:
            found = self.store.rows('SELECT * FROM attachment_parts WHERE attachment_id=? AND part=?', (row['id'], part))
            if not found:
                raise HTTPException(409, 'The file upload is incomplete. Retry the attachment.')
            item = found[0]
            raw = self.store.objects.read(item['reference'], item['size']) if item['reference'] else bytes(item['data'])
            if (len(raw) != min(row['part_size'], row['size']-position)
                    or len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']):
                raise HTTPException(503, 'Saved attachment part failed integrity verification.')
            checksum.update(raw)
            yield raw[max(0,offset-position):min(len(raw),end-position)]
            part += 1
            position += len(raw)
        if offset == 0 and end == row['size'] and row['upload_state'] == 'ready' and checksum.hexdigest() != row['sha256']:
            raise HTTPException(503, 'Saved attachment failed integrity verification.')

    def stage(self, attachment_id, owner_id):
        with self.store.connect() as conn:
            row = dict(self.owned(conn, attachment_id, owner_id))
        stream = tempfile.TemporaryFile()
        try:
            checksum = hashlib.sha256()
            for chunk in self.chunks(row):
                stream.write(chunk)
                checksum.update(chunk)
            stream.seek(0)
            return row, stream, checksum.hexdigest()
        except BaseException:
            stream.close()
            raise

    def finish(self, original, checksum, inspected, storage_limit, processing_error=''):
        from .attachments import public_file
        media_type, preview, preview_text = inspected
        preview_ref = self.store.objects.put(preview) if preview and self.store.objects.enabled else ''
        cutoff = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        with self.store.connect() as conn:
            conn.begin_write()
            row = self.owned(conn, original['id'], original['owner_id'])
            if row['upload_state'] == 'ready':
                if row['sha256'] != checksum:
                    raise HTTPException(409, 'The completed upload differs from this file.')
                return public_file(row)
            if any(row[key] != original[key] for key in ('name', 'size', 'created_at', 'part_size')):
                raise HTTPException(409, 'The file upload changed. Retry the attachment.')
            parts = conn.execute('SELECT part,size FROM attachment_parts WHERE attachment_id=? ORDER BY part', (row['id'],)).fetchall()
            count = (row['size']+row['part_size']-1)//row['part_size']
            if len(parts) != count or any(item['part'] != index or item['size'] != min(row['part_size'], row['size']-index*row['part_size'])
                                           for index,item in enumerate(parts)):
                raise HTTPException(409, 'The file upload is incomplete. Retry the attachment.')
            self.attachments.check_quota(conn, row['owner_id'], 0, len(preview), storage_limit, cutoff)
            conn.execute('''UPDATE attachments SET sha256=?,media_type=?,preview_text=?,preview=?,preview_ref=?,
                preview_size=?,processing_error=?,upload_state='ready' WHERE id=?''',
                (checksum, media_type, preview_text, b'' if preview_ref else preview, preview_ref,
                 len(preview), processing_error, row['id']))
            return public_file(conn.execute('SELECT * FROM attachments WHERE id=?', (row['id'],)).fetchone())
