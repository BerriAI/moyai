"""Run: uv run python scripts/memory_refresh_demo.py [--delay 3]

Real local broker requests and identity-worker code, with synthetic Slack
profiles and an advanced clock. No network, model calls or production data.
"""
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app import credentials, identities
from app.config import Settings
from app.main import create_app
from app.security import digest


async def demo(directory, delay):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8796',
                  auto_prepare_repositories=False, slack_bot_enabled=True,
                  google_client_id='demo-client', google_client_secret='demo-secret',
                  google_allowed_domains='berri.ai', google_admin_emails='demo@berri.ai')
    app = create_app(Settings(_env_file=None, **values))
    store = app.state.store
    team, user = 'T12345678', 'U12345678'
    store.identity({'method': 'google', 'identity': {'sub': 'demo', 'email': 'demo@berri.ai', 'name': 'Demo'}})
    with store.connect() as conn:
        actor = store.slack_identity_in(conn, team, user)
    app.state.connectors.save('slack', {'kind': 'oauth', 'access_token': 'unused-demo-token',
        'bot': {'access_token': 'demo-bot-token', 'scope': 'users:read,users:read.email',
                'team': {'id': team}, 'bot_user_id': 'U99999999'}}, 'Local demo')
    clock, profile_calls = [time.time()], []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0], tz)

    async def slack_profile(method, url, **kwargs):
        assert method == 'GET' and url == 'https://slack.com/api/users.info'
        profile_calls.append(kwargs['params']['user'])
        return {'ok': True, 'user': {'id': user, 'team_id': team,
                'profile': {'email': 'demo@berri.ai', 'real_name': 'Demo'}}}

    def new_run(prompt):
        run = store.create_run(prompt, '', 'modal', [], chat_enabled=True, user_id=actor)
        store.claim_message(run['id'])
        store.update_run(run['id'], status='running', token_hash=digest('demo-capability'))
        return store.run(run['id'])

    async def show(title, *lines):
        print('\n' + title, flush=True)
        for line in lines:
            print('  ' + line, flush=True)
        await asyncio.sleep(delay)

    print('MOYAI DEVIN | Automatic Slack memory refresh', flush=True)
    print('Real broker + worker. Synthetic Slack profile; clock advanced locally.', flush=True)
    print('$ uv run python scripts/memory_refresh_demo.py', flush=True)
    with patch.object(identities, 'time', SimpleNamespace(time=lambda: clock[0])), \
         patch.object(identities, 'now', lambda: Clock.now(timezone.utc).isoformat()), \
         patch.object(credentials, 'datetime', Clock), \
         patch.object(app.state.connectors, 'request', slack_profile):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=values['public_url'],
                                     headers={'Authorization': 'Bearer demo-capability'}) as client:
            async def tools(run):
                response = await client.get(f"/broker/{run['id']}/tools")
                response.raise_for_status()
                return [tool['name'] for tool in response.json() if tool['name'].startswith('memory_')]

            async def call(run, name, **arguments):
                response = await client.post(f"/broker/{run['id']}/tools/call", json={
                    'name': name, 'arguments': {'turn_id': run['active_message_id'], **arguments}})
                response.raise_for_status()
                return response.json()

            run = new_run('Use a 25-word TLDR.')
            store.execute("""UPDATE users SET email='demo@berri.ai',profile_eligible=1,
                linked_user_id='google:demo',link_status='linked',link_method='email',
                profile_checked_at=?,profile_next_check=? WHERE id=?""",
                (datetime.fromtimestamp(clock[0] - 7200, timezone.utc).isoformat(),
                 int(clock[0]) + 22 * 3600, actor))
            assert await tools(run) == []
            await show('1. Reproduce the existing daily-timer gap',
                       'Profile checked 2 hours ago; next daily refresh in 22 hours.',
                       'GET broker/tools -> HTTP 200 | 0 memory tools')

            await app.state.identities.sync_due()
            assert len(await tools(run)) == 3 and len(profile_calls) == 1
            saved = await call(run, 'memory_save', key='response-style', title='Response style',
                content='Use a 25-word TLDR.', request_id='demo-save-memory',
                source_message_id=run['active_message_id'], source_quote='Use a 25-word TLDR.')
            assert saved['saved']
            await show('2. The worker automatically refreshes the profile',
                       'GET broker/tools -> HTTP 200 | 3 memory tools',
                       'POST memory_save -> HTTP 200 | preference saved')

            for _ in range(4):
                clock[0] += 31 * 60
                assert len(await tools(run)) == 3
                await app.state.identities.sync_due()
                assert len(await tools(run)) == 3
            assert len(profile_calls) == 5
            await show('3. Advance through 2 hours of background refreshes',
                       '4 automatic refreshes; memory tools remain available.',
                       'No sign-in prompts or manual profile refreshes.')

            store.finish_message(run['id'], run['active_message_id'], 'Saved preference.')
            store.update_run(run['id'], status='idle', token_hash='')
            next_run = new_run('Recall my response style.')
            recalled = await call(next_run, 'memory_search', query='response style')
            assert recalled['loaded'] == 1
            assert '25-word TLDR' in app.state.memory.context(next_run)
            await show('4. Start another session as the same Slack user',
                       'POST memory_search -> HTTP 200 | 1 saved preference recalled',
                       'PASS: persistent memory survives the hourly boundary.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delay', type=float, default=0, help='Seconds to hold each result for a recording.')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='moyai-memory-demo-') as directory:
        asyncio.run(demo(directory, max(0, args.delay)))
