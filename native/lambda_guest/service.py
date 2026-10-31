"""Command and checkpoint transport for the native Pydantic workspace.

Port 80 must stay behind AWS authenticated MicroVM ingress. No model, Temporal,
AWS credentials or retired agent runtime runs in this guest.
"""
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

import checkpoint

ROOT = Path("/var/lib/moyai-runtime")
LOCK = threading.RLock()
JOBS = {}
PROCESSES = {}
FROZEN = {}
BUSY = None
ACTIVE = set()
BOOTSTRAPPED = sys.argv[1:] == ["bootstrapped"]
CACHE = checkpoint.ContentCache()
MAX_ARCHIVE = 4 * 1024**3


def identity(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Invalid operation identity")
    return value


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def process_state(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19], fields[0]
    except (FileNotFoundError, ProcessLookupError):
        return None, None


def thaw():
    for pid, started in list(FROZEN.items()):
        if process_state(pid)[0] == started:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
    FROZEN.clear()


def freeze():
    """The subreaper owns even double-forked commands; AWS daemons stay live."""
    try:
        for _ in range(30):
            processes = {}
            for path in Path("/proc").iterdir():
                if not path.name.isdigit():
                    continue
                try:
                    fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                    processes[int(path.name)] = (int(fields[1]), fields[19], fields[0])
                except (FileNotFoundError, ProcessLookupError):
                    continue
            owned = {os.getpid()}
            while True:
                children = {pid for pid, (parent, _, _) in processes.items() if parent in owned}
                if children <= owned:
                    break
                owned |= children
            changed = False
            for pid in owned - {os.getpid()}:
                _, started, state = processes[pid]
                if state != "Z" and pid not in FROZEN:
                    FROZEN[pid] = started
                    try:
                        os.kill(pid, signal.SIGSTOP)
                        changed = True
                    except ProcessLookupError:
                        pass
            if not changed and all(process_state(pid)[1] in {None, "T", "t", "Z"} for pid in FROZEN):
                return
            time.sleep(.02)
        raise RuntimeError("Commands did not quiesce")
    except BaseException:
        thaw()
        raise


def transfer(url, path, *, upload=False, checksum=None):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    if not url.startswith("https://"):
        raise ValueError("Transfer requires HTTPS")
    opener = urllib.request.build_opener(NoRedirect())
    if upload:
        with path.open("rb") as stream:
            request = urllib.request.Request(url, data=stream, method="PUT", headers={
                "Content-Length": str(path.stat().st_size),
                "x-amz-checksum-sha256": checksum, "x-amz-server-side-encryption": "AES256",
            })
            with opener.open(request, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError("Upload failed")
    else:
        with opener.open(url, timeout=60) as response, path.open("wb") as stream:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARCHIVE:
                    raise ValueError("Archive exceeds limit")
                stream.write(chunk)


def receipt(job_id, value):
    JOBS[job_id] = value
    atomic(ROOT / "operations" / job_id / "receipt.json", value)


def perform(path, body):
    global BUSY
    job_id = body["id"]
    directory = ROOT / "operations" / job_id
    try:
        if path == "/initialize":
            restored = body.get("restore")
            if restored:
                archive = directory / "restore.tar.gz"
                transfer(restored["url"], archive)
                if archive.stat().st_size != restored["size"]:
                    raise ValueError("Archive length mismatch")
                checkpoint.restore(archive, expected_sha256=restored["sha256"])
                archive.unlink()
            atomic(ROOT / "initialized.json", {"id": job_id})
            result = {"ready": True}
        elif path == "/checkpoint":
            freeze()
            def progress(value):
                with LOCK:
                    if JOBS[job_id]["state"] == "running":
                        JOBS[job_id]["progress"] = value
            result = checkpoint.pack(
                directory / "archive.tar.gz", max_bytes=MAX_ARCHIVE, content_cache=CACHE, progress=progress,
            )
        elif path == "/upload":
            archive = ROOT / "operations" / identity(body["checkpoint"]) / "archive.tar.gz"
            transfer(body["url"], archive, upload=True, checksum=body["checksum"])
            result = {"uploaded": True}
        else:
            raise ValueError("Unknown operation")
        with LOCK:
            if JOBS[job_id]["state"] != "failed":
                receipt(job_id, {"state": "done", "result": result})
            if path == "/initialize":
                BUSY = None
    except BaseException as error:
        with LOCK:
            receipt(job_id, {"state": "failed", "error": type(error).__name__})
            if path == "/upload" and BUSY:
                JOBS[BUSY] = {"state": "failed", "error": "UploadFailed"}
    finally:
        with LOCK:
            ACTIVE.discard(job_id)
            if not ACTIVE and BUSY and JOBS.get(BUSY, {}).get("state") == "failed" and path != "/initialize":
                thaw()
                BUSY = None


def expire(job_id, timeout):
    time.sleep(timeout)
    with LOCK:
        if BUSY == job_id:
            receipt(job_id, {"state": "failed", "error": "CheckpointExpired"})
            if not ACTIVE:
                thaw()


def start_job(path, body):
    global BUSY
    job_id = identity(body["id"])
    saved = ROOT / "operations" / job_id / "receipt.json"
    if job_id in JOBS:
        return JOBS[job_id]
    if saved.exists():
        return json.loads(saved.read_text())
    if path == "/initialize" and (ROOT / "initialized.json").exists():
        if json.loads((ROOT / "initialized.json").read_text())["id"] != job_id:
            raise RuntimeError("VM belongs to another launch")
        return {"state": "done", "result": {"ready": True}}
    if path == "/upload":
        if BUSY != body["checkpoint"] or JOBS[BUSY]["state"] != "done":
            raise RuntimeError("Checkpoint not ready")
    elif BUSY:
        raise RuntimeError("Workspace busy")
    else:
        BUSY = job_id
    receipt(job_id, {"state": "failed", "error": "UncertainOperation"})
    JOBS[job_id] = {"state": "running"}
    ACTIVE.add(job_id)
    threading.Thread(target=perform, args=(path, body), daemon=True).start()
    if path == "/checkpoint":
        threading.Thread(target=expire, args=(job_id, min(body["timeout"], 900)), daemon=True).start()
    return {"state": "running"}


def stop_process(job_id):
    process = PROCESSES.get(job_id)
    if process and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def command(body):
    job_id = identity(body["id"])
    directory = ROOT / "commands" / job_id
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    saved = directory / "receipt.json"
    if saved.exists():
        value = json.loads(saved.read_text())
        if value["fingerprint"] != fingerprint:
            raise ValueError("Command identity collision")
        return value
    directory.mkdir(parents=True, exist_ok=True)
    atomic(saved, {"fingerprint": fingerprint, "state": "uncertain"})
    with (directory / "stdout").open("wb") as stdout, (directory / "stderr").open("wb") as stderr:
        try:
            process = subprocess.Popen(
                body["command"], cwd="/workspace", env={**os.environ, **body.get("env", {})},
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True,
            )
        except FileNotFoundError:
            atomic(saved, {"fingerprint": fingerprint, "state": "done", "exit_code": 127})
            return {"state": "done"}
    PROCESSES[job_id] = process
    def wait():
        timed_out = False
        try:
            code = process.wait(timeout=body.get("timeout"))
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_process(job_id)
            code = process.wait()
        atomic(saved, {"fingerprint": fingerprint, "state": "done", "exit_code": code, "timed_out": timed_out})
    threading.Thread(target=wait, daemon=True).start()
    return {"state": "running"}


def dispatch(path, body):
    global BUSY
    with LOCK:
        if path == "/health":
            diagnostics = checkpoint.BASELINE.with_suffix(".diagnostics.json")
            return {
                "version": 1, "bootstrapped": BOOTSTRAPPED,
                "image_baseline": json.loads(diagnostics.read_text()) if diagnostics.exists() else None,
            }
        if not BOOTSTRAPPED:
            raise RuntimeError("Bootstrap required")
        if path == "/job":
            job_id = identity(body["id"])
            return JOBS.get(job_id) or json.loads((ROOT / "operations" / job_id / "receipt.json").read_text())
        if path == "/checkpoint/finish":
            if BUSY == identity(body["id"]):
                if ACTIVE:
                    raise RuntimeError("Checkpoint still running")
                thaw()
                shutil.rmtree(ROOT / "operations" / BUSY, ignore_errors=True)
                BUSY = None
            return {"ok": True}
        if path in {"/initialize", "/checkpoint", "/upload"}:
            return start_job(path, body)
        if BUSY or not (ROOT / "initialized.json").exists():
            raise RuntimeError("Workspace is not ready")
        if path == "/command":
            return command(body)
        if path == "/command/stop":
            stop_process(identity(body["id"]))
            return {"ok": True}
        if path == "/command/read":
            directory = ROOT / "commands" / identity(body["id"])
            saved = json.loads((directory / "receipt.json").read_text())
            result = {key: saved[key] for key in ("state", "exit_code", "timed_out") if key in saved}
            import base64
            for stream in ("stdout", "stderr"):
                with (directory / stream).open("rb") as handle:
                    offset = body.get(stream, 0)
                    if type(offset) is not int or offset < 0:
                        raise ValueError("Invalid stream offset")
                    handle.seek(offset)
                    result[stream] = base64.b64encode(handle.read(128 * 1024)).decode()
            if saved["state"] == "uncertain" and body["id"] not in PROCESSES:
                raise RuntimeError("Command outcome uncertain after service restart")
            return result
        raise ValueError("Unknown operation")


def lifecycle(action):
    """Native suspension preserves the interpreter and all command receipts."""
    global CACHE
    with LOCK:
        if action == "suspend":
            os.sync()
        elif action == "resume":
            if not BOOTSTRAPPED or not checkpoint.BASELINE.exists() or not (ROOT / "initialized.json").exists():
                raise RuntimeError("Cannot resume an uninitialized workspace")
            # Revalidate hashes after a wall-clock jump; do not re-exec or clear
            # command/process state. Model credentials stay on the trusted host,
            # and each transport request opens a fresh connection.
            CACHE = checkpoint.ContentCache()
        else:
            raise ValueError("Unknown lifecycle hook")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value):
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        ready = self.path in {"/health", "/aws/lambda-microvms/runtime/v1/ready"}
        self.reply(200 if ready else 404, {"version": 1, "bootstrapped": BOOTSTRAPPED})

    def do_POST(self):
        if self.path == "/aws/lambda-microvms/runtime/v1/ready":
            return self.reply(200, {"ready": True})
        if self.path in {"/aws/lambda-microvms/runtime/v1/suspend", "/aws/lambda-microvms/runtime/v1/resume"}:
            try:
                lifecycle(self.path.rsplit("/", 1)[1])
            except Exception:
                return self.reply(503, {"error": "Workspace lifecycle hook failed"})
            return self.reply(200, {"ready": True})
        if self.path == "/bootstrap":
            self.reply(200, {"ok": True})
            self.wfile.flush()
            if not BOOTSTRAPPED:
                os.execv(sys.executable, [sys.executable, __file__, "bootstrapped"])
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 2 * 1024 * 1024:
                return self.reply(413, {"error": "Request too large"})
            self.connection.settimeout(30)
            self.reply(200, dispatch(self.path, json.loads(self.rfile.read(length))))
        except FileNotFoundError:
            self.reply(404, {"error": "Operation not found"})
        except Exception:
            self.reply(409, {"error": "Workspace operation failed"})


if __name__ == "__main__":
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "Could not own guest processes")
    if not checkpoint.BASELINE.exists():
        if BOOTSTRAPPED:
            raise RuntimeError("Immutable image baseline missing")
        checkpoint.baseline()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()
