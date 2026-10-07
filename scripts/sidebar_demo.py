"""Local sidebar demo: real app + SQLite, synthetic identities and GitHub responses.

Run: uv run python scripts/sidebar_demo.py
Open http://localhost:8846/demo/login. No real SSO, Slack, model or GitHub calls.
"""
from pathlib import Path
import argparse
import json
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.responses import RedirectResponse
import uvicorn

from app.config import Settings
from app.main import create_app
from app.session_folders import FolderName


def demo(directory: Path):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=directory, public_url='http://localhost:8846',
                  google_client_id='local-fixture', google_client_secret='local-fixture',
                  google_allowed_domains='example.com', google_admin_emails='alex@example.com',
                  auto_prepare_repositories=False, session_titles_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    store = app.state.store
    identity = {'sub': 'sidebar-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'}
    owner = store.identity({'method': 'google', 'identity': identity})
    other = store.identity({'method': 'google', 'identity': {'sub': 'sidebar-sam', 'email': 'sam@example.com', 'name': 'Sam'}})
    store.execute("UPDATE organization SET name='Local demo' WHERE id=1")

    @app.get('/demo/login')
    async def login():
        response = RedirectResponse('/')
        app.state.security.new_session(response, identity=identity)
        return response

    credentials = {'kind': 'github_app', 'installation_id': 10, 'account_id': 44, 'repository_ids': [202]}
    repo = {'id': 202, 'full_name': 'example/project', 'owner': {'id': 44},
            'default_branch': 'main', 'html_url': 'https://github.com/example/project', 'private': True}
    github = app.state.connectors.github
    github.remember_repository(repo, credentials)
    app.state.connectors.save('github', credentials, 'Synthetic GitHub fixture')
    github.references_dirty = False

    async def token(*args, **kwargs):
        assert kwargs['repository'] == 202
        return 'local-fixture-only'

    async def request(method, path, **kwargs):
        assert method == 'GET' and path.startswith('/repositories/202/pulls/'), (method, path)
        number = int(path.rsplit('/', 1)[1])
        return {'number': number, 'html_url': f'https://github.com/example/project/pull/{number}',
                'title': f'Synthetic change {number}', 'state': 'closed' if number in (2, 3, 4) else 'open',
                'merged': number in (2, 3), 'draft': number == 5,
                'requested_reviewers': [{'id': 1}] if number == 6 else [], 'requested_teams': [], 'base': {'repo': repo}}

    github.installation_token = token
    github.request = request
    if not store.rows('SELECT id FROM runs'):
        titles = ['Improve gateway request tracing', 'Review retry behavior', 'Fix streaming response cleanup',
                  'Investigate shared Slack thread', 'Update deployment guide', 'Tune request timeouts']
        folder = app.state.session_folders.save(owner, FolderName(name='This week'))
        for index, title in enumerate(titles):
            run = store.create_run(title, '', 'demo', [], chat_enabled=True, user_id=other if index in (3, 4) else owner)
            message = store.claim_message(run['id'])
            store.finish_message(run['id'], message['id'],
                                 'This is a local sidebar demonstration with synthetic conversations and GitHub responses. Use the session menu to pin or unpin, move to a folder, or archive and restore.')
            store.update_run(run['id'], status='idle')
            store.execute('UPDATE runs SET display_title=? WHERE id=?', (title, run['id']))
            if index in (3, 4):
                store.execute("INSERT INTO messages(run_id,role,content,status,created_at,user_id) VALUES(?,'user','Joining this conversation','completed',?,?)", (run['id'], run['created_at'], owner))
            if index in (0, 1, 3):
                store.execute('INSERT INTO slack_events(event_id,run_id,channel,thread_ts,user_id,created_at) VALUES(?,?,?,?,?,?)',
                              (f'EDemo{index}', run['id'], 'CDEMO', f'1790000000.00000{index}', 'UDEMO', run['created_at']))
            numbers = {0: [1, 2, 4], 1: [6], 2: [3], 3: [5]}.get(index, [])
            for number in numbers:
                receipt = {'repository_id': 202, 'repository': repo['full_name'], 'number': number,
                           'url': f'https://github.com/example/project/pull/{number}', 'title': f'Synthetic change {number}'}
                store.execute('''INSERT INTO github_publications(id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (f'demo-{number}', run['id'], message['id'], 'fixture', 'fixture', json.dumps(receipt), github.connection_version(), run['created_at']))
            if index in (1, 2):
                app.state.session_folders.pin(owner, run['id'], True)
            if index == 5:
                app.state.session_folders.move(owner, run['id'], folder['id'])
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, help='Optional directory to retain demo pins and folders across restarts')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='moyai-sidebar-') as temporary:
        uvicorn.run(demo(args.data_dir or Path(temporary)), host='127.0.0.1', port=8846)
