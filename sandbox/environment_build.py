"""Detached, idempotent project builds. Runs only inside a Modal sandbox."""
import base64
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
try:
    from .detect_environment import detect, DetectionError
except ImportError:
    from detect_environment import detect, DetectionError

ROOT = Path('/tmp/moyai-environment-build')
REPO = Path('/workspace/repo')


def shell(script, cwd, *, timeout=1500):
    if script.strip():
        subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=cwd, check=True, timeout=timeout)


def build(recipe):
    token = os.environ.pop('MOYAI_CLONE_TOKEN', '')
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    if token:
        # GitHub's Git transport uses Basic auth, unlike its REST API.
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0='Authorization: Basic ' + auth)
    REPO.parent.mkdir(parents=True, exist_ok=True)
    print('Cloning ' + recipe['repository'] + ' at ' + recipe['ref'], flush=True)
    subprocess.run(['git', 'init', str(REPO)], check=True)
    url = 'https://github.com/' + recipe['repository'] + '.git'
    subprocess.run(['git', '-C', str(REPO), 'remote', 'add', 'origin', url], check=True)
    # Fetch accepts branches, tags and full SHAs; arguments never pass through a shell.
    subprocess.run(['git', '-C', str(REPO), 'fetch', '--depth', '1', 'origin', recipe['ref']], env=env, check=True, timeout=180)
    env.pop('GIT_CONFIG_VALUE_0', None)
    token = ''
    subprocess.run(['git', '-C', str(REPO), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
    sha = subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip()
    # Publication still rechecks live repository access and ancestry on the server.
    (REPO / '.git/moyai.json').write_text(json.dumps({'repository_id': recipe.get('repository_id'), 'repository': recipe['repository'],
        'base_sha': sha, 'default_branch': recipe['ref']}))
    if recipe.get('setup_mode') == 'detect':
        recipe = detect(REPO, recipe)
        if recipe.get('apt_packages'):
            subprocess.run(['apt-get', 'update'], check=True, timeout=180)
            subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends', *recipe['apt_packages']], check=True, timeout=600)
        print('Resolved repository setup', flush=True)
    try:
        for name in ('setup', 'startup', 'verify'):
            print('\n== ' + name + ' ==', flush=True)
            shell(recipe[name], REPO, timeout=1800 if name == 'setup' else 300)
    finally:
        print('\n== shutdown before snapshot ==', flush=True)
        shell(recipe['shutdown'], REPO, timeout=90)
    return sha, recipe


def supervise():
    with (ROOT / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # A completed process or an interrupted ambiguous build is never replayed.
        if (ROOT / 'started').exists():
            return
        (ROOT / 'started').write_text('1')
        with (ROOT / 'output.log').open('a', buffering=1) as log:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            try:
                sha, recipe = build(json.loads(Path('/tmp/moyai-environment.json').read_text()))
                result = {'done': True, 'success': True, 'commit_sha': sha, 'recipe': recipe}
            except Exception as exc:
                print('Build failed: ' + type(exc).__name__, flush=True)
                if isinstance(exc, DetectionError):
                    print(str(exc), flush=True)
                result = {'done': True, 'success': False, 'error': 'Project setup or validation failed. Inspect the build log and edit the recipe.'}
            temp = ROOT / 'result.tmp'
            temp.write_text(json.dumps(result))
            temp.replace(ROOT / 'result.json')


def main(action):
    ROOT.mkdir(exist_ok=True)
    if action == 'start':
        subprocess.Popen([sys.executable, __file__, 'supervise'], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'started': True}
    if action == 'status':
        result = json.loads((ROOT / 'result.json').read_text()) if (ROOT / 'result.json').exists() else {'done': False}
        path = ROOT / 'output.log'
        if path.exists():
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 48000))
                result['log'] = stream.read(48000).decode(errors='replace')
        return result
    if action == 'supervise':
        supervise()
        return {}
    raise ValueError('Unknown build action')


if __name__ == '__main__':
    print(json.dumps(main(sys.argv[1])))
