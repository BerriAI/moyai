"""Prepare a mounted data directory, then exec the server as an unprivileged user."""
import os
from pathlib import Path
import pwd
import sys


def prepare_data_directory(directory: Path, uid: int, gid: int) -> None:
    # Only the dedicated data tree is adopted. Never follow a link out of it.
    app_directory = Path(__file__).resolve().parent
    if (not directory.is_absolute() or directory.resolve() != directory
            or directory == app_directory or directory in app_directory.parents):
        raise ValueError("DATA_DIR must be a dedicated absolute directory, without symlinks.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(directory, uid, gid, follow_symlinks=False)
    for _, directories, files, root_fd in os.fwalk(directory, follow_symlinks=False):
        for name in directories + files:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (info.st_uid, info.st_gid) != (uid, gid):
                os.chown(name, uid, gid, dir_fd=root_fd, follow_symlinks=False)


def server_command() -> list[str]:
    if os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get('RENDER_SERVICE_ID'):
        return [sys.executable, str(Path(__file__).with_name("render_start.py"))]
    return [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0",
            "--port", os.environ.get("PORT", "8787"), "--workers", "1",
            "--timeout-graceful-shutdown", "20"]


def main() -> None:
    directory = Path(os.environ.setdefault("DATA_DIR", "/data"))
    if os.getuid() == 0:
        user = pwd.getpwnam("workspace")
        prepare_data_directory(directory, user.pw_uid, user.pw_gid)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
        os.environ["HOME"] = user.pw_dir
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError(f"DATA_DIR {directory} must be writable by UID {os.getuid()}.")
    print(f"Starting server as UID {os.getuid()} with DATA_DIR={directory}", flush=True)
    command = sys.argv[1:] or server_command()
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
