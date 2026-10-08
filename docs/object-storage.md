# Private file storage and database backups

Moyai keeps sessions, messages, permissions, spending records and file metadata in SQLite. With private object storage configured, new upload originals, image previews, result ZIPs and browser captures go to an S3-compatible bucket. SQLite holds their references. Existing authenticated Moyai URLs, draft ownership, agent scope and media range requests continue to work.

Without this configuration, the existing local storage behavior remains. Increasing the Render disk to 10 GB gives recovery headroom; it does not move existing bytes or set up object storage. SQLite still grows with structured history. Monitor database volume usage separately.

## Configure the destination

Provision a private bucket using AWS S3, Cloudflare R2 or a compatible service. Block public access; do not enable public ACLs or automatic object expiration. Configure these values in the deployment's secret store:

```dotenv
OBJECT_STORAGE_BUCKET=<private-bucket>
OBJECT_STORAGE_ENDPOINT=https://<s3-compatible-endpoint>
OBJECT_STORAGE_REGION=us-east-1
OBJECT_STORAGE_PREFIX=moyai
OBJECT_STORAGE_ACCESS_KEY_ID=<dedicated-key>
OBJECT_STORAGE_SECRET_ACCESS_KEY=<dedicated-secret>
```

Leave `OBJECT_STORAGE_ENDPOINT` empty for AWS S3's normal regional endpoint; use the provider's required region for other services, such as `auto` for R2. `OBJECT_STORAGE_SESSION_TOKEN` supports temporary AWS credentials. AWS workload credentials may be used instead of explicit keys. The app requires object read and write access under `<prefix>/blobs/`; it does not require public access or delete permission. All objects use server-generated SHA-256 keys. Keep bucket encryption enabled according to your provider's policy.

Object references bind the endpoint, bucket and prefix, so preserve those values when rotating credentials or restarting the app. Changing the destination does not migrate objects. The same configuration and object population must remain available to a restored database. A missing configuration, missing object or failed integrity check returns a storage error; Moyai does not silently return an empty file or switch a failed remote write back onto the database disk.

## Roll out without losing legacy files

1. Keep the existing disk, database and encryption/session keys. Upgrade **all app writers** to the object-storage-aware release and configure the same private destination for them. Keep the single-instance SQLite deployment requirement. Do not start an older binary against this database after publishing remote references; it cannot read those files.
2. Verify a new upload, image preview, archive download and browser recording through Moyai, then restart and verify them again. The deployment configuration alone does not prove credentials, bucket policy or persistence.
3. Make a verified external database backup before migrating existing payloads. Preserve any legacy artifact directory and existing operator backup/audit files separately until they are verified externally too.
4. Inspect the migration plan, then run bounded batches. Default migration preserves every source BLOB and local file. Repeated runs resume safely; already-published references are skipped. Only acknowledged run ZIPs, frozen agent handoff ZIPs and valid run capture filenames are considered. Temporary files, symlinks and unrelated files are excluded.

Run these commands as the database file's owner (UID 10001 in the Docker/Render deployment), with the application's configuration. Even a read-only root SQLite connection can create WAL/SHM sidecars with the wrong owner, so every operation checks ownership before opening the database. Use the deployed environment or a correctly configured operator environment:

```sh
python -m app.storage_maintenance plan --data-dir /var/data/moyai
python -m app.storage_maintenance backup --data-dir /var/data/moyai
python -m app.storage_maintenance migrate --data-dir /var/data/moyai --limit 100
```

Omitting the operation defaults to `plan`. The plan opens the existing SQLite database read-only, does not upgrade its schema and does not contact object storage. `schema_ready: false` means the storage-aware application must be deployed before migration. Plan byte counts are logical payload sizes; frozen hard links can be counted under more than one name, and database allocation, WAL and unrelated maintenance files are not included.

Migration uploads and reads back each payload before publishing references. It rechecks the attachment row or artifact revision under the database writer lock; a concurrent newer version wins. Network operations occur before that lock. A failed batch can leave verified earlier records and unreferenced objects; rerunning the command is safe. An error does not establish that the whole batch rolled back. The JSON result contains progress and the remaining plan. Continue until the attachment and legacy-artifact `pending` counts reach zero.

## Reclaim existing local space separately

Once externally verified backups and restored-download checks pass, an operator can explicitly clear retained SQLite attachment BLOBs:

```sh
python -m app.storage_maintenance migrate --data-dir /var/data/moyai --limit 100 --clear-attachment-blobs
```

This re-verifies the remote bytes, rechecks the current row and then atomically clears only the matching retained BLOBs. It preserves metadata, messages, users, remote objects and local artifact files. Repeat until `retained_bytes` for attachments is zero. It does **not** shrink the SQLite file: freed pages are available for subsequent database writes.

No command here removes local artifact files, deletes object-store content or runs `VACUUM`. If physical disk reclamation is needed, schedule separate offline maintenance after obtaining a verified external backup and confirming enough free temporary space. Stop every database writer before compaction. For each local artifact selected for removal, confirm a current manifest entry, verify its corresponding object and account for frozen hard links; do not delete an entire artifact tree merely because some files migrated. Preserve uncertain or unacknowledged files for inspection. Session deletion continues to retain historical payloads under the existing retention behavior.

## Backup and restore

`backup` uses SQLite's backup API, including committed WAL contents, then integrity-checks the snapshot, compresses it and uploads it privately. Staging uses the system temporary directory, never `DATA_DIR`. On Render, keep `TMPDIR` off the persistent `/var/data` mount and provide enough temporary capacity for an uncompressed snapshot plus its compressed copy. The command verifies the uploaded object's full size and hash before returning a JSON receipt with `reference`, `sha256`, `size`, `format` and `created_at`. Save that receipt outside the database volume. The receipt contains no credentials.

Database backups contain conversation history and encrypted application credentials; treat them as private. They are not added to downloadable session artifacts. Preserve the encryption key and session secret separately in a protected secret store. A database backup is not a backup of legacy files or the referenced object population. Keep the bucket available and apply any backup retention policy to complete recoverable sets, not to blob age alone: old content can still be referenced by a new database snapshot.

To restore into an isolated, stopped instance:

1. Download the receipt's object using the configured provider tooling. Its key is `<OBJECT_STORAGE_PREFIX>/blobs/<sha256>`. Compare the compressed file size and SHA-256 with the receipt before opening it.
2. Decompress it into an empty staging directory as `workspace.db`. Run SQLite's `PRAGMA integrity_check` and require `ok` before replacing anything. Use the storage-aware app release and the original private destination configuration.
3. Restore legacy files that have not migrated and the separate encryption/session secrets. Verify uploaded originals/previews, saved archives, a frozen agent handoff and captures from the staged restore.
4. Stop all production writers before any cutover. Preserve the current database and its WAL consistently rather than mixing the restored database with old sidecars. Only switch to the checked restore after those checks pass.

There is no automatic production restore, bucket provisioning, retention deletion or scheduled backup in this change.

## Existing maintenance backups and audit files

The migration command deliberately excludes arbitrary maintenance directories. Export them separately as a private operator archive, outside the user-visible artifact manifest. First stop the maintenance writer or make a consistent snapshot. Choose the exact source directory and stage the archive on a different volume with sufficient capacity. The following example includes only regular files, skips symlinks, preserves relative names and refuses more than 10,000 files or 1 GiB of source data:

```sh
python - /var/data/moyai/maintenance /tmp/moyai-maintenance.tar.gz <<'PY'
import os, pathlib, sys, tarfile
source, target = map(pathlib.Path, sys.argv[1:])
if source.is_symlink() or not source.is_dir() or target.exists() or target.resolve().is_relative_to(source.resolve()):
    raise SystemExit('Choose a real source directory and a new temporary archive path')
files, size = [], 0
for path in source.rglob('*'):
    if any(parent.is_symlink() for parent in (path, *path.parents)) or not path.is_file():
        continue
    size += path.stat().st_size
    files.append(path)
    if len(files) > 10000 or size > 1024**3:
        raise SystemExit('Archive exceeds the reviewed export bound')
with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
    with tarfile.open(fileobj=output, mode='w:gz', dereference=False) as archive:
        for path in files:
            archive.add(path, arcname=str(path.relative_to(source)), recursive=False)
print(f'Archived {len(files)} files, {size} source bytes to {target}')
PY
```

Replace the example source with the directory found during the incident inventory; do not guess its location. Upload the archive to an explicitly chosen private backup destination using provider tooling, download it to a second temporary file, compare SHA-256 hashes, and inspect its relative filenames. Record the receipt and a restore check before considering removal of any original. Do not upload an active SQLite main file alone; use the `backup` command for that database. This export procedure is manual and is not a lifecycle rule on the application bucket.
