# Backup, restore, and health monitoring

[Documentation](README.md) · [Project overview](../README.md)

Moyai keeps its state in a single-writer SQLite database (`DATA_DIR/workspace.db`).
Modal deployments additionally checkpoint to a Modal Volume every two seconds;
Render deployments use a persistent disk instead. This guide covers the local
backup workflow that works on every host, verified restores, and health probes
for load balancers and monitoring.

## Quick start

```sh
# Nightly verified backup, keep the last 14 days
uv run python scripts/db_backup.py --data-dir /var/data/moyai --backup-dir /var/backups/moyai --keep 14

# Check integrity without writing anything
uv run python scripts/db_backup.py --data-dir /var/data/moyai --verify-only

# Restore a backup into a fresh data directory
uv run python scripts/db_backup.py --data-dir /var/data/moyai-restored --restore /var/backups/moyai/workspace-20260101-000000.db
```

The script exits `0` on success and `2` when the database is missing or corrupt.
It prints a JSON manifest to stdout so cron logs capture exactly what happened.
Backups are created with the SQLite backup API (committed WAL content included),
verified with `PRAGMA integrity_check` before publishing, stored with `0600`
permissions, and rotated to `--keep` files.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKUP_DIR` | _(unset)_ | Enables the `POST /api/admin/backup` endpoint and optional startup backups |
| `BACKUP_KEEP` | `7` | How many timestamped backups to retain (1–90) |
| `BACKUP_ON_STARTUP` | `false` | Take one verified backup on every server start when `BACKUP_DIR` is set |

```dotenv
BACKUP_DIR=/var/backups/moyai
BACKUP_KEEP=14
BACKUP_ON_STARTUP=true
```

Startup backups run in a background thread and never prevent the server from
starting; failures are logged with the backup reason.

## What gets backed up

- `workspace.db` — sessions, messages, events, connections, organization settings.
- Result archives under `DATA_DIR/artifacts/` are **not** copied by the backup
  tool; they are already replicated to the Modal Volume by checkpoints on Modal,
  and on Render they live on the same persistent disk. Copy the artifacts
  directory alongside the database when migrating hosts:

```sh
cp workspace-20260101-000000.db /var/data/moyai/workspace.db
cp -r artifacts /var/data/moyai/artifacts
```

Always restore into a stopped server with a single writer. The restore helper
quarantines the previous database as `workspace.pre-restore-<timestamp>.db`
and removes stale `-wal`/`-shm` files so the restored copy opens cleanly.

## Verified restores

Both startup paths validate before booting:

- Modal checkpoint restores (`CHECKPOINT_DIR`) verify the staged copy with
  `PRAGMA integrity_check` plus a required-table check (`runs`, `messages`,
  `events`, `connections`, `organization`). A corrupt checkpoint aborts startup
  with `Checkpoint failed integrity validation` instead of serving bad data.
- Render bootstrap imports validate integrity and refuse checkpoints with
  active sessions (unchanged behavior, now matched by the Modal path).

To test a backup before a cutover, restore into an empty directory and run:

```sh
uv run python scripts/db_backup.py --data-dir /tmp/moyai-verify --restore /var/backups/moyai/workspace-20260101-000000.db
uv run python scripts/db_backup.py --data-dir /tmp/moyai-verify --verify-only
```

## Health monitoring

`GET /health` keeps its `{"status": "ok"}` contract for existing load
balancers, but now returns `503` when the database fails integrity validation
or free disk space drops below 50 MB. Point Render, Docker `HEALTHCHECK`, and
uptime monitors at `/health`.

Administrators get full detail at `GET /api/health/detailed` (admin session
required):

```json
{
  "database": {"exists": true, "ok": true, "integrity": "ok", "errors": []},
  "stats": {"database_bytes": 123456, "rows": {"runs": 12, "messages": 34, "events": 56}},
  "disk_low": false,
  "checkpoint": {"configured": false, "exists": false},
  "ok": true,
  "reasons": []
}
```

`POST /api/admin/backup` (admin + CSRF) creates an on-demand backup and returns
the same manifest as the CLI. It returns `409` when `BACKUP_DIR` is unset,
`404` when no database exists, and `422` when the live database is corrupt.

## Reverting a Render deployment

The deployment guide asks operators to export the database before reverting to
the frozen Modal checkpoint. With this release that step is concrete:

```sh
uv run python scripts/db_backup.py --data-dir /var/data/moyai --backup-dir /var/backups/moyai-render
cp -r /var/data/moyai/artifacts /var/backups/moyai-render/artifacts
```

Keep that backup; the old Modal checkpoint is frozen and no longer current
once Render accepts new work.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `Refusing to back up a corrupt database` | Stop writes, run `--verify-only`, restore the newest passing backup into a fresh directory |
| `Checkpoint failed integrity validation` | Inspect the checkpoint volume, restore from `BACKUP_DIR`, never delete the quarantined file until recovery is confirmed |
| `/health` returns `503 low disk space` | Free disk or grow the volume; SQLite needs headroom for WAL and backups |
| Backup grows quickly | Lower `ATTACHMENT_STORAGE_LIMIT_MB` or `BACKUP_KEEP`; artifacts dominate size on media-heavy workspaces |
