"""Operator backup tool for Moyai's SQLite database.

Render and Docker operators were told to "export the current Render database
and artifacts first" before a revert, but no export tool existed. This script
fills that gap without requiring server credentials:

    uv run python scripts/db_backup.py --data-dir /var/data/moyai --backup-dir /var/backups/moyai
    uv run python scripts/db_backup.py --data-dir .data --backup-dir .backups --keep 14 --vacuum --verify-only

Exit codes: 0 on success, 1 on usage errors, 2 when the database is missing or
corrupt. Output is a JSON manifest on stdout so cron jobs and runbooks can log it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db_maintenance import backup_database, restore_backup, verify_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a verified Moyai database backup.")
    parser.add_argument("--data-dir", default=".data", type=Path, help="Moyai DATA_DIR containing workspace.db")
    parser.add_argument("--backup-dir", default=".backups", type=Path, help="Directory for timestamped backups")
    parser.add_argument("--keep", default=7, type=int, help="How many recent backups to retain (1-90)")
    parser.add_argument("--vacuum", action="store_true", help="Compact the database while backing up")
    parser.add_argument("--verify-only", action="store_true", help="Run integrity_check without writing a backup")
    parser.add_argument("--restore", default=None, type=Path, help="Restore this backup file into --data-dir instead of backing up")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir, backup_dir = args.data_dir, args.backup_dir
    if args.restore is not None:
        try:
            result = restore_backup(args.restore, data_dir)
        except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
            return 2
        print(json.dumps({"ok": True, **result}, indent=2), flush=True)
        return 0
    if args.verify_only:
        checked = verify_database(data_dir / "workspace.db")
        print(json.dumps(checked, indent=2), flush=True)
        return 0 if checked["ok"] else 2
    if not 1 <= args.keep <= 90:
        print("--keep must be between 1 and 90", file=sys.stderr)
        return 1
    try:
        manifest = backup_database(data_dir, backup_dir, keep=args.keep, vacuum=args.vacuum)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 2
    except (RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 2
    print(json.dumps({"ok": True, **manifest}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
