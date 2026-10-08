"""Local PR-panel fixture using Moyai's real session APIs and temporary SQLite.

Run: uv run python scripts/pull_request_panel_demo.py --port 8846
Sign-in and publication receipts are synthetic. No model or cloud calls are made.
With --live-github, PR details are read from GitHub using the local gh login.
Otherwise GitHub data is a deterministic fixture. No GitHub writes are allowed.
Use --static-root PATH to compare a pristine frontend with the same data.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi.responses import RedirectResponse
from app.config import Settings
from app import main


IDENTITY = {'sub': 'pr-panel-demo', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'}
REPOSITORY = 'BerriAI/moyai'
PULLS = [
    (144, 'Export reported reasoning tokens', 'open', False),
    (145, 'Fix Modal client accumulation in workspace monitoring', 'closed', False),
    (135, 'Export cache usage and provider identifiers', 'closed', False),
]


def demo(directory, port=8976, static_root=None, computer_container=None, live_github=False):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url=f'http://localhost:{port}',
                  google_client_id='local-fixture', google_client_secret='local-fixture',
                  google_allowed_domains='example.com', google_admin_emails=IDENTITY['email'],
                  auto_prepare_repositories=False, session_titles_enabled=False, demo_step_seconds=0.01)
    if static_root:
        main.STATIC = Path(static_root).resolve(strict=True)
    app = main.create_app(Settings(_env_file=None, **values))
    # Cookies ignore ports; isolate this fixture from other localhost previews.
    cookie_name = f'moyai_pr_demo_{port}'
    @app.middleware('http')
    async def isolated_cookie(request, call_next):
        cookies = [(key, value) for key, value in request.cookies.items() if key != 'workspace_session']
        value = request.cookies.get(cookie_name)
        if value:
            cookies.append(('workspace_session', value))
        request.scope['headers'] = [(key, value) for key, value in request.scope['headers'] if key != b'cookie']
        request.scope['headers'].append((b'cookie', '; '.join(f'{key}={value}' for key, value in cookies).encode()))
        if hasattr(request, '_cookies'):
            del request._cookies
        response = await call_next(request)
        response.raw_headers = [(key, value.replace(b'workspace_session=', cookie_name.encode() + b'=', 1)
                                 if key == b'set-cookie' else value) for key, value in response.raw_headers]
        return response

    store = app.state.store
    repository = (json.loads(subprocess.run(['gh', 'api', 'repos/' + REPOSITORY], check=True, capture_output=True).stdout)
                  if live_github else {'id': 202, 'full_name': REPOSITORY, 'owner': {'id': 42}, 'default_branch': 'main',
                                       'html_url': 'https://github.com/' + REPOSITORY, 'private': True})
    repo_id = repository['id']
    credentials = {'kind': 'github_app', 'installation_id': 10, 'account_id': repository['owner']['id'], 'repository_ids': [repo_id]}
    github = app.state.connectors.github
    github.remember_repository(repository, credentials)
    app.state.connectors.save('github', credentials, 'Local PR demo')
    async def token(*args, **kwargs):
        return 'local-fixture'
    async def github_read(method, path, **kwargs):
        if method != 'GET' or not (path == f'/repositories/{repo_id}' or path.startswith(f'/repositories/{repo_id}/pulls/')):
            raise RuntimeError('The demo supports only read-only repository and PR requests.')
        if live_github:
            command = ['gh', 'api', path, '--method', 'GET']
            for key, value in kwargs.get('params', {}).items():
                command.extend(['-f', f'{key}={value}'])
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await process.communicate()
            if process.returncode:
                from app.connector_errors import ConnectorError
                raise ConnectorError('The local GitHub read failed. Check gh authentication.')
            return json.loads(stdout)
        if path == f'/repositories/{repo_id}':
            return repository
        number = int(path.split('/')[4])
        if path.endswith('/files'):
            return [{'filename': 'app/usage.py', 'status': 'modified', 'additions': 2, 'deletions': 1,
                     'patch': '@@ -12,2 +12,3 @@\n-def usage():\n+def usage(response):\n+    return response.usage\n '},
                    {'filename': 'tests/test_usage.py', 'status': 'added', 'additions': 2, 'deletions': 0,
                     'patch': '@@ -0,0 +1,2 @@\n+def test_reported_usage():\n+    assert usage(response) == response.usage'}]
        return {'number': number, 'html_url': f'https://github.com/{REPOSITORY}/pull/{number}',
                'title': next((title for n, title, _, _ in PULLS if n == number), 'Demo pull request'),
                'state': 'open' if number == 144 else 'closed', 'draft': False, 'merged': number != 144,
                'requested_reviewers': [], 'requested_teams': [], 'created_at': '2026-10-07T12:00:00Z',
                'body': '## Changes\n\nPreserve reported usage from the provider.\n\n## Verification\n\nFocused regression checks pass.',
                'base': {'repo': repository, 'ref': 'main'}, 'head': {'sha': 'a' * 40, 'ref': 'litellm_reported_usage'},
                'user': {'login': 'demo-contributor'}, 'additions': 4, 'deletions': 1, 'changed_files': 2}
    github.installation_token = token
    github.request = github_read
    owner = store.identity({'method': 'google', 'identity': IDENTITY})

    def session(prompt, answer, display_title='', parent_run_id='', agent_label=''):
        run = store.create_run(prompt, 'https://github.com/' + REPOSITORY, 'demo', ['github'],
                               chat_enabled=True, user_id=owner, github_repository_id=repo_id)
        message = store.claim_message(run['id'])
        store.finish_message(run['id'], message['id'], answer)
        store.update_run(run['id'], status='idle')
        store.execute('UPDATE runs SET display_title=?,parent_run_id=?,agent_label=? WHERE id=?',
                      (display_title, parent_run_id, agent_label, run['id']))
        return run, message['id']

    root, root_message = session(
        'Keep my pull requests next to this conversation',
        'This is a local UI fixture: the session and publication receipts are synthetic. ' +
        ('PR details are live read-only GitHub data. ' if live_github else 'PR details use sample GitHub data. ') +
        'No model, cloud workspace or GitHub writes are used.\n\n'
        'The session has two pull requests and a direct child has one more:\n\n'
        + '\n'.join(f'- [{title}](https://github.com/{REPOSITORY}/pull/{number})'
                    for number, title, _, _ in PULLS)
        + '\n\nOpen a pull request while keeping this conversation and the composer visible.',
        display_title='Review the workspace improvements')
    child, child_message = session('Check keyboard navigation', 'Local child-session fixture.',
                                    parent_run_id=root['id'], agent_label='Keyboard navigation')
    group_id = uuid4().hex
    store.execute("INSERT INTO agent_groups(id,parent_id,message_id,request_key,payload,status,created_at) VALUES(?,?,?,'toolbox-demo','{}','completed',?)",
                  (group_id, root['id'], root_message, root['created_at']))
    failed, _ = session('Check narrow layouts', 'Local failed child fixture.',
                        parent_run_id=root['id'], agent_label='Responsive layouts')
    running, _ = session('Review session updates', 'Local running child fixture.',
                         parent_run_id=root['id'], agent_label='Live updates')
    for agent in (child, failed, running):
        store.execute('UPDATE runs SET agent_group_id=? WHERE id=?', (group_id, agent['id']))
    store.update_run(failed['id'], status='failed')

    @app.post('/demo/agent-state/{status}')
    async def agent_state(status: str):
        if status not in {'running', 'completed', 'failed', 'idle'}:
            raise HTTPException(400, 'Unknown fixture status')
        store.update_run(running['id'], status=status)
        return {'id': running['id'], 'status': status}

    unrelated, unrelated_message = session('Investigate another change', 'Local unrelated-session fixture.',
                                            display_title='Unrelated session')
    for index, (number, title, state, draft) in enumerate(PULLS + [(999, 'Unrelated pull request', 'open', False)]):
        source, message_id = (root, root_message) if index < 2 else (
            (child, child_message) if index == 2 else (unrelated, unrelated_message))
        receipt = {'repository': REPOSITORY, 'repository_id': repo_id, 'number': number,
                   'url': f'https://github.com/{REPOSITORY}/pull/{number}', 'title': title,
                   'state': state, 'draft': draft, 'merged': state == 'closed'}
        store.execute('''INSERT INTO github_publications
            (id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at)
            VALUES(?,?,?,?,?,?,?,?)''',
            (f'demo-pr-{number}', source['id'], message_id, 'local-fixture', f'moyai/demo-{number}',
             json.dumps(receipt), 'local-fixture', f'2026-10-07T12:0{index}:00+00:00'))

    # Without a local browser, exercise a sleeping cloud workspace without
    # contacting a provider. It has no sandbox ID and cannot start cloud work.
    store.execute("UPDATE runs SET mode='modal' WHERE id=?", (root['id'],))
    if computer_container:
        # Only transport is local: all Computer auth, CSRF, tab/lease logic and
        # Chromium pages are the real implementation. Never use a production run.
        store.execute("UPDATE runs SET mode='modal',sandbox_id='local-browser-fixture' WHERE id=?", (root['id'],))
        async def execute(container, *args, timeout=45):
            process = await asyncio.create_subprocess_exec(
                'docker', 'exec', computer_container, 'python', '/opt/workspace-runner/computer.py', *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode:
                raise RuntimeError('Local browser container failed: ' + stderr.decode()[-300:])
            return json.loads(stdout)
        async def request(body):
            return await execute(None, 'request', json.dumps(body), timeout=530)
        local_browser = SimpleNamespace(computer_request=request)
        async def sandbox(run):
            return local_browser
        app.state.computer.sandbox = sandbox
        app.state.computer.execute = execute

    @app.get('/demo/login')
    async def login():
        response = RedirectResponse('/#run=' + root['id'])
        app.state.security.new_session(response, identity=IDENTITY)
        return response

    app.state.demo_run_id = root['id']
    app.state.demo_unrelated_run_id = unrelated['id']
    print(f'Local fixture: http://localhost:{port}/demo/login', flush=True)
    print(f'Session: http://localhost:{port}/#run={root["id"]}', flush=True)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8976)
    parser.add_argument('--static-root', type=Path)
    parser.add_argument('--live-github', action='store_true', help='Read real PRs with the local gh login; no writes')
    parser.add_argument('--computer-container', help='Disposable local container running the real sandbox browser')
    options = parser.parse_args()
    with TemporaryDirectory(prefix='pull-request-panel-') as directory:
        uvicorn.run(demo(directory, options.port, options.static_root, options.computer_container, options.live_github), host='127.0.0.1', port=options.port)
