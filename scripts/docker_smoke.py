"""Exercise the built Docker image with disposable volumes; no cloud credentials.

    docker build -t moyai-docker-smoke .
    python scripts/docker_smoke.py moyai-docker-smoke --output /tmp/docker-evidence
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4


SEED = """
import os
from pathlib import Path
from app.db import Store
from app.config import Settings
from app.security import Security

directory = Path('/var/data/moyai')
store = Store(directory)
settings = Settings(_env_file=None, data_dir=directory)
security = Security(settings)
run = store.create_run('preserved conversation', '', 'modal', [], chat_enabled=True)
store.update_run(run['id'], status='idle', snapshot_id='im-preserved')
store.execute("UPDATE messages SET status='completed'")
store.execute("INSERT INTO connections VALUES('linear',?,'Test connection','now')",
              (security.fernet.encrypt(b'fake-credential').decode(),))
archives = directory / 'artifacts'
archives.mkdir(mode=0o700)
(archives / 'preserved.zip').write_bytes(b'unchanged archive')
# A link outside DATA_DIR must never grant the app ownership of its target.
Path('/var/data/outside').mkdir()
Path('/var/data/outside/keep').write_text('outside')
(directory / 'outside-link').symlink_to('/var/data/outside', target_is_directory=True)
for root, dirs, files in os.walk(directory):
    os.chown(root, 1000, 1000)
    os.chmod(root, 0o700)
    for name in files:
        os.chown(Path(root) / name, 1000, 1000)
        os.chmod(Path(root) / name, 0o600)
print('Seeded native-Python-style disk: UID 1000, private database, keys and archive.')
"""

VERIFY = """
import os
from pathlib import Path
from app.db import Store
from app.config import Settings
from app.security import Security
directory = Path('/var/data/moyai')
store = Store(directory)
run = store.rows('SELECT * FROM runs')[0]
assert run['prompt'] == 'preserved conversation' and run['snapshot_id'] == 'im-preserved'
security = Security(Settings(_env_file=None, data_dir=directory))
encrypted = store.rows('SELECT encrypted FROM connections')[0]['encrypted']
assert security.fernet.decrypt(encrypted.encode()) == b'fake-credential'
assert (directory / 'artifacts/preserved.zip').read_bytes() == b'unchanged archive'
store.execute("UPDATE runs SET summary='write-after-migration' WHERE id=?", (run['id'],))
for path in [directory, directory / 'workspace.db', directory / 'encryption.key']:
    assert path.stat().st_uid == 10001
    assert path.stat().st_mode & 0o077 == 0
assert Path('/var/data/outside').stat().st_uid == 0
assert Path('/var/data/outside/keep').stat().st_uid == 0
status = Path('/proc/1/status').read_text().splitlines()
assert next(line for line in status if line.startswith('Uid:')).split()[1:] == ['10001'] * 4
assert next(line for line in status if line.startswith('Gid:')).split()[1:] == ['10001'] * 4
print('PASS: sessions, encrypted connection, keys and archive survive; SQLite writes work.')
print('PASS: PID 1 runs as UID/GID 10001; private modes and external symlink targets unchanged.')
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--output", type=Path, default=Path("/tmp/moyai-docker-evidence"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    recording = (args.output / "docker-smoke.cast").open("w")
    recording.write(json.dumps({"version": 2, "width": 110, "height": 32, "timestamp": int(time.time())}) + "\n")
    transcript = []

    def log(message):
        print(message, flush=True)
        transcript.append(message)
        recording.write(json.dumps([round(time.monotonic() - started, 3), "o", message.replace("\n", "\r\n") + "\r\n"]) + "\n")
        recording.flush()

    def docker(*arguments, check=True):
        result = subprocess.run(["docker", *arguments], text=True, capture_output=True, timeout=90)
        if check and result.returncode:
            raise RuntimeError(f"Docker command failed: {result.stderr or result.stdout}")
        return result

    prefix = "moyai-smoke-" + uuid4().hex[:8]
    containers, volumes = [], []
    python = "/app/.venv/bin/python"
    common = ["-e", "WORKSPACE_PASSWORD=synthetic-smoke-password", "-e", "SESSION_TITLES_ENABLED=false"]

    def start(name, volume, *environment):
        containers.append(name)
        docker("run", "-d", "--name", name, "-v", f"{volume}:/var/data", *common,
               "-e", "DATA_DIR=/var/data/moyai", *environment, args.image)

    def healthy(name):
        for _ in range(40):
            if docker("exec", "--user", "10001:10001", name, python, "/app/docker_healthcheck.py", check=False).returncode == 0:
                return
            if docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip() == "false":
                break
            time.sleep(0.5)
        raise RuntimeError("Server never became healthy: " + docker("logs", name).stdout + docker("logs", name).stderr)

    try:
        log("$ python scripts/docker_smoke.py " + args.image)
        volume = prefix + "-legacy"
        volumes.append(volume)
        docker("volume", "create", volume)
        log(docker("run", "--rm", "--user", "0", "--entrypoint", python, "-v", f"{volume}:/var/data",
                   args.image, "-c", SEED).stdout.strip())
        log("Reproduce the old startup against that disk as UID 10001...")
        failure = docker("run", "--rm", "--user", "10001:10001", "--entrypoint", python,
                         "-v", f"{volume}:/var/data", args.image, "-c",
                         "import sqlite3; sqlite3.connect('/var/data/moyai/workspace.db')", check=False)
        assert failure.returncode != 0 and "unable to open database file" in failure.stderr
        log("CONFIRMED: sqlite3.OperationalError: unable to open database file")
        name = prefix + "-render"
        log("Start fixed image: Render origin, PORT=12345, same private disk...")
        start(name, volume, "-e", "RENDER_EXTERNAL_URL=https://render.example", "-e", "PUBLIC_URL=https://stale.example",
              "-e", "PORT=12345", "-e", "RENDER_MIGRATION_STAGE=false", "-e", "BOOTSTRAP_MODAL_VOLUME=")
        healthy(name)
        log("PASS: /health returns HTTP 200 on PORT=12345 using the Render origin.")
        for operation in ("from app.db import Store; Store", "from app.storage_maintenance import plan; plan"):
            root_access = docker("exec", "--user", "0", name, python, "-c",
                                 "from pathlib import Path; " + operation + "(Path('/var/data/moyai'))", check=False)
            assert root_access.returncode != 0 and "Database access requires UID 10001" in root_access.stderr
        log("PASS: root maintenance is rejected before opening the live private database.")
        log(docker("exec", "--user", "10001:10001", name, python, "-c", VERIFY).stdout.strip())
        docker("stop", "--time", "30", name)
        assert docker("inspect", "-f", "{{.State.ExitCode}}", name).stdout.strip() == "0"
        docker("start", name)
        healthy(name)
        docker("exec", "--user", "10001:10001", name, python, "-c",
               "import sqlite3; db=sqlite3.connect('/var/data/moyai/workspace.db'); "
               "assert db.execute('SELECT summary FROM runs').fetchone()[0] == 'write-after-migration'")
        log(docker("exec", "--user", "10001:10001", name, python, "-c", VERIFY).stdout.strip())
        log("PASS: graceful stop and restart preserve the same database and credentials.")
        docker("stop", "--time", "30", name)

        name = prefix + "-private"
        start(name, volume, "-e", "RENDER_SERVICE_ID=srv-private-demo",
              "-e", "MOYAI_PUBLIC_URL=https://private.example", "-e", "RENDER_MIGRATION_STAGE=false",
              "-e", "CLOUDFLARE_ACCESS_TEAM_DOMAIN=demo.cloudflareaccess.com",
              "-e", "CLOUDFLARE_ACCESS_AUDIENCE=employee", "-e", "CLOUDFLARE_ACCESS_BROKER_AUDIENCE=broker")
        healthy(name)
        docker("exec", "--user", "10001:10001", name, python, "-c",
               "import httpx; r=httpx.get('http://127.0.0.1:10000/api/credentials', headers={'Host':'private.example'}); "
               "assert r.status_code == 401 and 'Cloudflare Access' in r.text")
        log(docker("exec", "--user", "10001:10001", name, python, "-c", VERIFY).stdout.strip())
        log("PASS: private Render startup uses its custom origin, preserves data, and blocks anonymous access.")
        docker("stop", "--time", "30", name)

        empty = prefix + "-empty"
        volumes.append(empty)
        docker("volume", "create", empty)
        name = prefix + "-missing"
        start(name, empty, "-e", "RENDER_EXTERNAL_URL=https://render.example", "-e", "RENDER_MIGRATION_STAGE=false")
        assert docker("wait", name).stdout.strip() != "0"
        logs = docker("logs", name)
        assert "refusing an empty workspace" in logs.stdout + logs.stderr
        log("PASS: Render refuses to create an empty replacement workspace when its database is missing.")

        name = prefix + "-staging"
        start(name, empty, "-e", "RENDER_EXTERNAL_URL=https://render.example", "-e", "RENDER_MIGRATION_STAGE=true")
        healthy(name)
        log("PASS: migration staging starts successfully on Render's default port 10000.")
        name = prefix + "-generic"
        start(name, empty, "-e", "PUBLIC_URL=https://vm.example")
        healthy(name)
        log("PASS: ordinary Docker startup still works on port 8787.")
        log("All Docker smoke checks passed. No cloud credentials or production data used.")
    finally:
        for name in containers:
            docker("rm", "-f", name, check=False)
        for volume in volumes:
            docker("volume", "rm", volume, check=False)
        (args.output / "docker-smoke.txt").write_text("\n".join(transcript) + "\n")
        recording.close()


if __name__ == "__main__":
    main()
