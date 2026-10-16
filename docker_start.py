"""Prepare a mounted data directory, then exec the server as an unprivileged user."""
import os
from pathlib import Path
import pwd
import sys


def selected_core() -> str:
    """Keep the selector dependency-free in the legacy entry interpreter."""
    core = os.environ.get("MOYAI_CORE", "legacy").strip()
    if core not in {"legacy", "pydantic"}:
        raise ValueError("MOYAI_CORE must be legacy or pydantic.")
    return core


def native_python() -> Path:
    executable = Path(__file__).resolve().parent / "native" / ".venv" / "bin" / "python"
    if not executable.is_file():
        raise RuntimeError("The locked native interpreter is missing; rebuild the image with the native product extra.")
    return executable


def ensure_selected_interpreter() -> None:
    """Direct Render invocations also switch before importing native services."""
    if selected_core() == "pydantic":
        executable = native_python()
        if Path(sys.prefix).resolve() != executable.parent.parent.resolve():
            os.execv(str(executable), [str(executable), *sys.argv])


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
    core = selected_core()
    executable = str(native_python()) if core == "pydantic" else sys.executable
    if os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get('RENDER_SERVICE_ID'):
        return [executable, str(Path(__file__).with_name("render_start.py"))]
    if core == "pydantic":
        host = "127.0.0.1" if os.environ.get("MOYAI_PRODUCT_LOCAL_REHEARSAL") == "true" else "0.0.0.0"
        return [executable, "-m", "moyai.product_start", "serve", "--host", host,
                "--port", os.environ.get("PORT", "8787")]
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
