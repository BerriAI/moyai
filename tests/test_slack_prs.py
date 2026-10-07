import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.connector_errors import ConnectorError
from app.github import Publish
from app.main import create_app
from app.slack_prs import SlackPullRequests
from test_github import PAYLOAD, GitHubAPI, connected


ROOT = '1790718000.123456'
FOLLOWUP = '1790719000.654321'


@pytest.fixture
def pr_env(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, slack_bot_enabled=True,
                        slack_signing_secret='test-signing-secret', slack_session_users='*',
                        auto_prepare_repositories=False, session_titles_enabled=False)
    app = create_app(settings)
    run_id, _ = connected(app)
    store, connectors = app.state.store, app.state.connectors
    connectors.save('slack', {'kind': 'oauth', 'access_token': 'user-token-not-for-sends',
        'bot': {'access_token': 'test-bot-token', 'team': {'id': 'T12345678'},
                'bot_user_id': 'U99999999', 'scope': 'reactions:write'}}, 'Test workspace')
    store.execute('''INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts)
        VALUES('T12345678','C12345678',?,?,?)''', (ROOT, run_id, FOLLOWUP))
    github = connectors.github
    api = GitHubAPI(github, monkeypatch)
    original_request = api.request
    env = SimpleNamespace(app=app, store=store, connectors=connectors, github=github, api=api,
                          worker=app.state.slack.pull_requests, run_id=run_id,
                          reactions=[], delivered=set(), missing_emoji=False,
                          lose='', error='', on_send=None, on_read=None, extra_prs={})

    async def request(method, path, **kw):
        if method == 'GET' and '/pulls/' in path:
            api.calls.append((method, path, kw))
            if env.on_read:
                env.on_read()
            number = int(path.rsplit('/', 1)[1])
            return copy.deepcopy(api.pr if number == 100 else env.extra_prs[number])
        return await original_request(method, path, **kw)
    monkeypatch.setattr(github, 'request', request)

    async def slack_request(method, url, **kw):
        assert method == 'POST' and url == 'https://slack.com/api/reactions.add'
        assert kw['headers']['Authorization'] == 'Bearer test-bot-token'
        payload = kw['json']
        env.reactions.append(payload)
        if env.on_send:
            env.on_send()
        if env.error:
            raise ConnectorError(env.error)
        if env.missing_emoji and payload['name'] != 'link' and payload['name'] != 'white_check_mark':
            return {'ok': False, 'error': 'invalid_name'}
        key = (payload['channel'], payload['timestamp'], payload['name'])
        if key in env.delivered:
            assert 'already_reacted' in kw['allowed_errors']
            return {'ok': False, 'error': 'already_reacted'}
        env.delivered.add(key)
        if env.lose == payload['name']:
            env.lose = ''
            raise ConnectorError('Response lost after Slack accepted the reaction')
        return {'ok': True}
    monkeypatch.setattr(connectors, 'request', slack_request)

    async def publish():
        result = await github.publish(store.run(run_id), Publish.model_validate(PAYLOAD))
        api.pr['merged'] = False
        return result
    env.publish = publish
    env.due = lambda: store.execute('UPDATE slack_pr_reactions SET next_check=0')
    env.rows = lambda: store.rows('SELECT * FROM slack_pr_reactions')
    return env


async def test_publication_and_merge_react_on_original_root_after_session_finishes(pr_env):
    e = pr_env
    result = await e.publish()
    assert len(e.rows()) == 1
    assert e.rows()[0]['thread_ts'] == ROOT != FOLLOWUP
    assert await e.publish() == result
    assert len(e.rows()) == 1
    e.store.update_run(e.run_id, status='completed', token_hash='')
    await e.worker.sync()
    assert e.reactions == [{'channel': 'C12345678', 'timestamp': ROOT, 'name': 'pr'}]
    assert e.rows()[0]['opened_reaction'] == 'pr' and not e.rows()[0]['finished']
    assert e.api.token_repository == 101
    assert any(c[0] == 'GET' and c[1] == '/repositories/101/pulls/100' for c in e.api.calls)
    e.api.pr.update(state='closed', merged=True)
    e.due()
    await e.worker.sync()
    assert [r['name'] for r in e.reactions] == ['pr', 'white_check_mark']
    assert {r['timestamp'] for r in e.reactions} == {ROOT}
    assert e.rows()[0]['finished'] == 1
    reads = len(e.api.calls)
    e.due()
    await SlackPullRequests(e.app.state.slack).sync()
    assert len(e.api.calls) == reads and len(e.reactions) == 2


async def test_closed_unmerged_is_not_success_and_can_reopen(pr_env):
    e = pr_env
    await e.publish()
    e.api.pr.update(state='closed', merged=False)
    await e.worker.sync()
    assert [r['name'] for r in e.reactions] == ['pr']
    assert e.rows()[0]['next_check'] > time.time() + 3500
    assert not e.rows()[0]['finished']
    e.api.pr.update(state='open', merged=False)
    e.due()
    await e.worker.sync()
    assert e.rows()[0]['next_check'] < time.time() + 61
    e.api.pr.update(state='closed', merged=True)
    e.due()
    await e.worker.sync()
    assert e.rows()[0]['finished'] == 1


@pytest.mark.parametrize('emoji', ['pr', 'white_check_mark'])
async def test_lost_reaction_response_recovers_after_restart_without_duplicate(pr_env, emoji):
    e = pr_env
    await e.publish()
    e.api.pr.update(state='closed', merged=True)
    e.lose = emoji
    await e.worker.sync()
    assert not e.rows()[0]['finished']
    assert e.rows()[0]['next_check'] > time.time()
    count = len(e.reactions)
    await e.worker.sync()
    assert len(e.reactions) == count
    e.due()
    await SlackPullRequests(e.app.state.slack).sync()
    assert e.rows()[0]['finished'] == 1
    assert len(e.delivered) == 2
    assert [r['name'] for r in e.reactions].count(emoji) == 2
    assert len([c for c in e.api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1


async def test_missing_custom_emoji_falls_back_to_builtin_link(pr_env):
    e = pr_env
    e.app.state.settings.slack_pr_reaction = 'pull_request'
    e.missing_emoji = True
    await e.publish()
    await e.worker.sync()
    assert [r['name'] for r in e.reactions] == ['pull_request', 'link']
    assert e.rows()[0]['opened_reaction'] == 'link'
    e.due()
    await e.worker.sync()
    assert len(e.reactions) == 2


async def test_slack_failure_does_not_fallback_or_finish_tracking(pr_env):
    e = pr_env
    await e.publish()
    e.error = 'missing_scope or rate_limited'
    await e.worker.sync()
    assert [r['name'] for r in e.reactions] == ['pr']
    assert not e.rows()[0]['opened_reaction'] and not e.rows()[0]['finished']


@pytest.mark.parametrize('change', ['paused', 'binding', 'team', 'bot', 'scope', 'slack_disabled',
                                   'slack_read_only', 'github_disabled', 'github_replaced', 'chat_disabled'])
async def test_pauses_and_replaced_connections_prevent_reactions(pr_env, change):
    e = pr_env
    await e.publish()
    if change == 'paused':
        e.store.execute('UPDATE slack_threads SET paused=1')
    elif change == 'binding':
        e.store.execute("UPDATE slack_threads SET thread_ts='1790000000.111111'")
    elif change in {'team', 'bot', 'scope'}:
        creds = await e.connectors.credentials('slack')
        if change == 'team':
            creds['bot']['team']['id'] = 'TOTHER999'
        elif change == 'bot':
            creds['bot']['bot_user_id'] = 'UOTHER999'
        else:
            creds['bot']['scope'] = 'chat:write'
        e.connectors.save('slack', creds, 'Changed')
    elif change == 'github_replaced':
        e.connectors.save('github', {'kind': 'github_app', 'installation_id': 20, 'repository_ids': [101]}, 'Replaced')
    elif change == 'chat_disabled':
        e.app.state.settings.slack_thread_chat_enabled = False
    else:
        provider = 'github' if change == 'github_disabled' else 'slack'
        e.store.execute('INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                        (provider, int(change.endswith('read_only')), int(change.endswith('read_only'))))
    await e.worker.sync()
    assert not e.reactions and not e.rows()[0]['finished']


async def test_sleep_and_wake_defer_but_preserve_tracking(pr_env):
    e = pr_env
    await e.publish()
    e.store.execute('UPDATE slack_threads SET paused=1')
    await e.worker.sync()
    assert not e.reactions
    e.store.execute('UPDATE slack_threads SET paused=0')
    await e.worker.sync()
    assert [r['name'] for r in e.reactions] == ['pr']


async def test_rechecks_identity_after_bot_token_refresh(pr_env, monkeypatch):
    e = pr_env
    await e.publish()
    original = e.connectors.slack_bot_token
    async def token():
        result = await original()
        e.store.execute('UPDATE slack_threads SET paused=1')
        return result
    monkeypatch.setattr(e.connectors, 'slack_bot_token', token)
    await e.worker.sync()
    assert not e.reactions


async def test_rechecks_policy_after_github_read_before_merge_reaction(pr_env):
    e = pr_env
    await e.publish()
    e.api.pr.update(state='closed', merged=True)
    e.on_read = lambda: e.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
    await e.worker.sync()
    assert [r['name'] for r in e.reactions] == ['pr']
    assert not e.rows()[0]['finished']


@pytest.mark.parametrize('bad', ['number', 'base', 'head', 'branch', 'merged_type', 'state', 'open_merged'])
async def test_merge_requires_matching_pr_identity_and_strict_state(pr_env, bad):
    e = pr_env
    await e.publish()
    e.api.pr.update(state='closed', merged=True)
    if bad == 'number':
        e.api.pr['number'] = 200
    elif bad in {'base', 'head'}:
        e.api.pr[bad]['repo']['id'] = 202
    elif bad == 'branch':
        e.api.pr['head']['ref'] = 'unrelated'
    elif bad == 'merged_type':
        e.api.pr['merged'] = 'false'
    elif bad == 'open_merged':
        e.api.pr['state'] = 'open'
    else:
        del e.api.pr['state']
    await e.worker.sync()
    assert [r['name'] for r in e.reactions] == ['pr']
    assert not e.rows()[0]['finished']


async def test_child_publication_routes_to_parent_original_root(pr_env):
    e = pr_env
    parent = e.store.create_run('Parent task', '', 'modal', ['github', 'slack'])
    e.store.execute('UPDATE slack_threads SET run_id=?', (parent['id'],))
    e.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent['id'], e.run_id))
    await e.publish()
    assert e.rows()[0]['run_id'] == parent['id']
    await e.worker.sync()
    assert e.reactions[0]['timestamp'] == ROOT


async def test_web_only_publication_and_arbitrary_answer_links_do_not_create_targets(pr_env):
    e = pr_env
    e.store.execute('DELETE FROM slack_threads')
    await e.publish()
    e.store.update_run(e.run_id, summary='https://github.com/BerriAI/litellm/pull/100')
    await e.worker.sync()
    assert not e.rows() and not e.reactions


async def test_unconfirmed_publication_never_enqueues_reaction(pr_env):
    e = pr_env
    e.api.status = 'diverged'
    with pytest.raises(ConnectorError):
        await e.publish()
    assert not e.rows()
    await e.worker.sync()
    assert not e.reactions


async def test_receipt_and_reaction_tracking_are_atomic(pr_env, monkeypatch):
    import app.github
    e = pr_env
    def fail(*args):
        raise RuntimeError('Database write failed')
    monkeypatch.setattr(app.github, 'track_publication', fail)
    with pytest.raises(RuntimeError):
        await e.publish()
    assert e.store.rows('SELECT result FROM github_publications')[0]['result'] == ''
    assert not e.rows()


async def test_multiple_prs_in_one_thread_track_merges_independently(pr_env):
    e = pr_env
    result = await e.publish()
    # A second verified publication from the same session shares the root.
    second = {**result, 'number': 101, 'url': result['url'].replace('/100', '/101'),
              'branch': result['branch'] + '-second'}
    e.extra_prs[101] = copy.deepcopy(e.api.pr)
    e.extra_prs[101].update(number=101)
    e.extra_prs[101]['head']['ref'] = second['branch']
    with e.store.connect() as conn:
        conn.execute('''INSERT INTO github_publications
            (id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at)
            SELECT 'second',run_id,message_id,arguments_hash,?,?,connection_version,created_at
            FROM github_publications''', (second['branch'], json.dumps(second)))
        from app.slack_prs import track_publication
        track_publication(conn, 'second', e.connectors.slack_installation())
    await e.worker.sync()
    assert len(e.delivered) == 1 and len(e.rows()) == 2
    e.extra_prs[101].update(state='closed', merged=True)
    e.due()
    await e.worker.sync()
    assert len(e.delivered) == 2
    assert sum(row['finished'] for row in e.rows()) == 1
    e.api.pr.update(state='closed', merged=True)
    e.due()
    await e.worker.sync()
    assert len(e.delivered) == 2
    assert all(row['finished'] for row in e.rows())


async def test_worker_lifecycle_starts_once_and_stops(pr_env):
    e = pr_env
    await e.publish()
    e.worker.recover()
    task = e.worker.watcher
    e.worker.recover()
    assert e.worker.watcher is task
    for _ in range(20):
        if e.reactions:
            break
        await asyncio.sleep(.01)
    await e.worker.shutdown()
    assert e.reactions and task.done()
