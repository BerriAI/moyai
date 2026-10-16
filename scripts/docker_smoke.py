"""Check the real image using disposable volumes and synthetic credentials only."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

SEED = '''
import os
from pathlib import Path
root = Path('/var/data/moyai')
root.mkdir(mode=0o700)
(root / 'preserved.txt').write_text('saved before restart')
Path('/var/data/outside').mkdir()
Path('/var/data/outside/keep').write_text('outside')
(root / 'outside-link').symlink_to('/var/data/outside', target_is_directory=True)
os.chown(root / 'preserved.txt', 1000, 1000)
os.chown(root, 1000, 1000)
'''
VERIFY = '''
import os
from pathlib import Path
import httpx
root = Path('/var/data/moyai')
assert (root / 'preserved.txt').read_text() == 'saved before restart'
assert root.stat().st_uid == 10001 and root.stat().st_mode & 0o077 == 0
assert Path('/var/data/outside/keep').stat().st_uid == 0
status = Path('/proc/1/status').read_text().splitlines()
assert next(line for line in status if line.startswith('Uid:')).split()[1:] == ['10001'] * 4
with httpx.Client(base_url='http://127.0.0.1:8787') as client:
    assert client.get('/api/me').status_code == 401
    login = client.post('/api/login', json={'username':'admin','password':'synthetic-smoke-password'})
    login.raise_for_status()
    client.headers['x-moyai-csrf'] = login.json()['csrf']
    sessions = client.get('/api/sessions'); sessions.raise_for_status()
    if not sessions.json():
        created = client.post('/api/sessions', json={'title':'Preserved browser task'})
        created.raise_for_status()
        connection = client.post('/api/connections', json={
            'name':'Synthetic connection','kind':'github','credential':'synthetic-never-used-token'})
        connection.raise_for_status()
    else:
        assert [row['title'] for row in sessions.json()] == ['Preserved browser task']
        connections = client.get('/api/connections'); connections.raise_for_status()
        assert [row['name'] for row in connections.json()] == ['Synthetic connection']
        assert 'synthetic-never-used-token' not in connections.text
    index = client.get('/'); index.raise_for_status()
    assert '/assets/' in index.text
print('PASS: login, CSRF, saved sessions, redacted connections, assets and UID 10001')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('--output', type=Path, default=Path('/tmp/moyai-docker-evidence'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    prefix = 'moyai-smoke-' + uuid4().hex[:8]
    containers, volumes, evidence = [], [], []
    python = '/app/.venv/bin/python'

    def docker(*arguments, check=True):
        result = subprocess.run(['docker', *arguments], capture_output=True, text=True, timeout=90, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result

    def record(message):
        print(message, flush=True)
        evidence.append(message)

    def start(suffix, volume, *environment, command=()):
        name = prefix + suffix
        containers.append(name)
        docker('run', '-d', '--name', name, '-v', f'{volume}:/var/data',
               '-e', 'DATA_DIR=/var/data/moyai', '-e', 'WORKSPACE_PASSWORD=synthetic-smoke-password',
               *environment, args.image, *command)
        return name

    def healthy(name):
        for _ in range(240):
            if docker('exec', '--user', '10001:10001', name, python, '/app/docker_healthcheck.py', check=False).returncode == 0:
                return
            if docker('inspect', '-f', '{{.State.Running}}', name).stdout.strip() == 'false':
                break
            time.sleep(.5)
        raise RuntimeError('Server did not become healthy: ' + docker('logs', name).stderr)

    try:
        volume = prefix + '-data'; volumes.append(volume)
        docker('volume', 'create', volume)
        docker('run', '--rm', '--entrypoint', python, '-v', f'{volume}:/var/data', args.image, '-c', SEED)
        name = start('-app', volume, '-e', 'LITELLM_API_KEY=synthetic-not-used',
                     command=(python, '-m', 'moyai.cli', 'dev', '--host', '0.0.0.0', '--port', '8787'))
        healthy(name)
        record(docker('exec', '--user', '10001:10001', name, python, '-c', VERIFY).stdout.strip())
        docker('stop', '--time', '30', name)
        assert docker('inspect', '-f', '{{.State.ExitCode}}', name).stdout.strip() == '0'
        docker('start', name); healthy(name)
        record(docker('exec', '--user', '10001:10001', name, python, '-c', VERIFY).stdout.strip())
        record('PASS: graceful restart preserves the same mounted data and sign-in')
        docker('stop', '--time', '30', name)
        empty = prefix + '-empty'; volumes.append(empty)
        docker('volume', 'create', empty)
        staged = start('-staged', empty, '-e', 'RENDER_EXTERNAL_URL=https://synthetic.example',
                       '-e', 'RENDER_MIGRATION_STAGE=true', '-e', 'PORT=12345')
        healthy(staged)
        docker('exec', staged, python, '-c',
               "import httpx; r=httpx.post('http://127.0.0.1:12345/api/sessions',json={}); assert r.status_code==503")
        record('PASS: Render maintenance uses the configured port and refuses new work')
        missing = start('-missing', empty, '-e', 'RENDER_SERVICE_ID=synthetic',
                        '-e', 'MOYAI_PUBLIC_URL=https://synthetic.example', '-e', 'RENDER_MIGRATION_STAGE=false')
        assert docker('wait', missing).stdout.strip() != '0'
        record('PASS: distributed startup fails closed without existing-product configuration')
    finally:
        for name in containers:
            logs = docker('logs', name, check=False)
            (args.output / (name + '.log')).write_text(logs.stdout + logs.stderr)
            docker('rm', '-f', name, check=False)
        for volume in volumes:
            docker('volume', 'rm', volume, check=False)
        (args.output / 'docker-smoke.txt').write_text('\n'.join(evidence) + '\n')


if __name__ == '__main__':
    main()
