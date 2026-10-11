"""Editable starter recipes, never executed on the web host."""

TEMPLATES = [{
    'name': 'LiteLLM development',
    'repository': 'BerriAI/litellm',
    'ref': 'main',
    'apt_packages': ['postgresql', 'postgresql-client', 'libpq-dev', 'libsndfile1', 'curl'],
    'setup': '''python -m pip install uv==0.12.22
uv python install 3.13
# Editable installs compile LiteLLM's native extension. Bound parallelism and
# omit dev debug symbols so its linker fits the 2 CPU / 8 GiB build sandbox.
CARGO_BUILD_JOBS=2 CARGO_PROFILE_DEV_DEBUG=0 uv sync --frozen --python 3.13 --extra proxy --extra extra_proxy --no-default-groups
uv pip install --python .venv/bin/python pytest==8.4.2 pytest-asyncio==1.2.0 playwright==1.58.0
export PATH="$PWD/.venv/bin:$PATH"
# Shell exports do not survive build phases or filesystem snapshots. LiteLLM
# loads .env in worker tools too; preserve an existing project's configuration.
if ! test -e .env; then
  printf '%s\\n' 'DATABASE_URL=postgresql://moyai_dev:local-development-only@127.0.0.1:5432/moyai_dev' > .env
fi
# Generate the database client for this exact checkout.
.venv/bin/prisma generate --schema=schema.prisma
''',
    'startup': '''service postgresql start
pg_isready -h 127.0.0.1 -p 5432
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='moyai_dev'" | grep -q 1; then
  runuser -u postgres -- psql -c "CREATE ROLE moyai_dev LOGIN PASSWORD 'local-development-only' CREATEDB"
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='moyai_dev'" | grep -q 1; then
  runuser -u postgres -- createdb -O moyai_dev moyai_dev
fi
''',
    'verify': '''export PATH="$PWD/.venv/bin:$PATH"
.venv/bin/python -c "import litellm; import litellm.proxy.proxy_server; print('LiteLLM proxy imports successfully')"
.venv/bin/prisma db push --schema=schema.prisma --skip-generate
.venv/bin/python - <<'PY'
import os
import psycopg
from dotenv import load_dotenv
load_dotenv('.env')
with psycopg.connect(os.environ['DATABASE_URL']) as conn:
    # Keep fixtures outside the public schema managed by Prisma, so repeating
    # verification does not make Prisma try to drop populated fixture tables.
    conn.execute('CREATE SCHEMA IF NOT EXISTS moyai_benchmark')
    conn.execute('CREATE TABLE IF NOT EXISTS moyai_benchmark.cases (id integer PRIMARY KEY, prompt text NOT NULL)')
    conn.execute("INSERT INTO moyai_benchmark.cases SELECT n, 'Synthetic benchmark case ' || n FROM generate_series(1,100) n ON CONFLICT DO NOTHING")
    count = conn.execute('SELECT count(*) FROM moyai_benchmark.cases').fetchone()[0]
    assert count == 100
    print('Postgres ready with 100 synthetic benchmark case records')
PY
.venv/bin/python - <<'PY'
import json, os, secrets, subprocess, time, urllib.request
master_key = 'sk-' + secrets.token_hex(32)
key = ''
def request(path, token=master_key, data=None, timeout=15):
    req = urllib.request.Request('http://127.0.0.1:18000' + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)
with open('/tmp/moyai-proxy-smoke.log', 'w') as log:
    process = subprocess.Popen(['.venv/bin/python', '-m', 'uvicorn', 'litellm.proxy.proxy_server:app', '--host', '127.0.0.1', '--port', '18000'],
        env={**os.environ, 'LITELLM_MASTER_KEY': master_key}, stdout=log, stderr=log)
    try:
        for attempt in range(60):
            if process.poll() is not None:
                raise RuntimeError('LiteLLM proxy exited during startup')
            try:
                readiness = request('/health/readiness', timeout=2)
            except OSError:
                time.sleep(1)
                continue
            if readiness.get('db') != 'connected':
                raise RuntimeError('LiteLLM proxy database is not connected')
            break
        else:
            raise RuntimeError('LiteLLM proxy did not become healthy')
        key = request('/key/generate', data={'key_alias': 'moyai-environment-smoke'})['key']
        try:
            assert request('/key/info', token=key)['info']['key_alias'] == 'moyai-environment-smoke'
        finally:
            request('/key/delete', data={'keys': [key]})
        print('Real LiteLLM proxy database connected; key create/read/delete passed')
    except Exception:
        output = open('/tmp/moyai-proxy-smoke.log').read()[-6000:].replace(master_key, '[smoke master key]')
        print(output.replace(key, '[smoke key]') if key else output)
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
PY
''',
    'shutdown': 'service postgresql stop',
    'instructions': '''The repository is /workspace/repo; use its .venv/bin/python and .venv/bin/litellm.
For subprocesses such as Prisma, export PATH="/workspace/repo/.venv/bin:$PATH".
Postgres runs locally on port 5432. Development-only DATABASE_URL:
postgresql://moyai_dev:local-development-only@127.0.0.1:5432/moyai_dev
The repository .env supplies this default to LiteLLM, including after restore.
The schema is initialized and moyai_benchmark.cases contains 100 synthetic case records.
These are small starter fixtures, not production-representative traffic. Create task-specific data for a benchmark.
For before/after work, resolve exact base/head SHAs and use separate worktrees and equivalent isolated databases.
Dependencies match the prepared commit. After changing revisions, refresh dependencies for that revision in a separate virtual environment.
Start a real proxy with .venv/bin/litellm --config <your-config.yaml> --port <port>.
Set a fresh local LITELLM_MASTER_KEY when starting a proxy; never disable its authentication checks.
Use the existing credential tools for model access if needed; no provider keys are baked into this environment.
Chromium and Playwright are available from the base image. Check process startup and health before measurements.
''',
}, {
    'name': 'Custom project', 'repository': 'BerriAI/moyai', 'ref': 'main',
    'apt_packages': [], 'setup': '', 'startup': '', 'verify': 'git status --short',
    'shutdown': '', 'instructions': 'Repository: /workspace/repo. Add the project build and test commands here.',
}, {
    'name': 'Repository development', 'repository': '', 'ref': 'HEAD', 'setup_mode': 'detect',
    'apt_packages': [], 'setup': '', 'startup': '', 'verify': 'git rev-parse --verify HEAD',
    'shutdown': '', 'instructions': '',
}]
