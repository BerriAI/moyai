import hashlib
import json

from app import captures, pr_delivery
from test_computer import PNG, WEBM
from test_slack import slack_app
from test_slack_chat import publication, start
from test_workspace import workspace


def test_pr_cards_require_a_receipt_from_this_run_or_a_direct_child(workspace):
    app, client = workspace
    store = app.state.store
    runs = [store.create_run('PR handoff', '', 'demo', [])['id'] for _ in range(4)]
    parent, other, child, grandchild = runs
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent, child))
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (child, grandchild))
    urls = [publication(app, run_id, number) for number, run_id in enumerate(runs, 100)]
    answer = '\n'.join(f'[View PR]({url})' for url in urls)
    with store.connect() as conn:
        selected = pr_delivery.select_prs(conn, parent, answer)
        assert [pr.url for pr in selected] == [urls[0], urls[2]]
        # A child cannot borrow its parent's publication receipt.
        assert [pr.url for pr in pr_delivery.select_prs(conn, child, answer)] == [urls[2], urls[3]]
    for run_id, expected in [(parent, [urls[0], urls[2]]), (child, [urls[2], urls[3]])]:
        data = client.get(f'/api/runs/{run_id}').json()
        assert data['pull_requests'] == data['pr_summary']['pull_requests']
        assert [pr['url'] for pr in data['pull_requests']] == expected


def test_session_pr_list_alias_tracks_publications_and_deleted_children(workspace):
    app, client = workspace
    store = app.state.store
    parent = store.create_run('Discuss https://github.com/BerriAI/moyai/pull/999', '', 'demo', [])['id']
    child = store.create_run('Child publication', '', 'demo', [])['id']
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent, child))
    data = client.get(f'/api/runs/{parent}').json()
    assert data['pull_requests'] == data['pr_summary']['pull_requests'] == []
    url = publication(app, child, title='A confirmed change')
    data = client.get(f'/api/runs/{parent}').json()
    assert data['pull_requests'] == data['pr_summary']['pull_requests']
    assert [(pr['url'], pr['title']) for pr in data['pull_requests']] == [(url, 'A confirmed change')]
    store.execute("UPDATE runs SET deleted_at='deleted' WHERE id=?", (child,))
    data = client.get(f'/api/runs/{parent}').json()
    assert data['pull_requests'] == data['pr_summary']['pull_requests'] == []
    store.execute("UPDATE runs SET deleted_at='' WHERE id=?", (child,))
    data = client.get(f'/api/runs/{parent}').json()
    assert data['pull_requests'] == data['pr_summary']['pull_requests']
    assert [pr['url'] for pr in data['pull_requests']] == [url]


def test_pr_selection_requires_the_exact_url_and_supports_legacy_receipts(workspace):
    app, _ = workspace
    store = app.state.store
    run_id = store.create_run('Legacy PR handoff', '', 'demo', [])['id']
    url = publication(app, run_id)
    legacy = {'number': 100, 'repository': 'berriai/moyai', 'url': url}
    store.execute('UPDATE github_publications SET result=? WHERE run_id=?', (json.dumps(legacy), run_id))
    with store.connect() as conn:
        for answer in [url + '0', url + '/files', url + '?fake=1',
                       url.replace('github.com', 'github.com.evil.example'),
                       'https://github.com/BerriAI/moyai/pull/999']:
            assert pr_delivery.select_prs(conn, run_id, answer) == []
        selected = pr_delivery.select_prs(conn, run_id, f'<{url}|View PR>\n{url}.')
        assert len(selected) == 1
        assert selected[0].url == url and selected[0].title == 'Pull request'
    # Even a stored result must agree with its own repository and PR number.
    store.execute('UPDATE github_publications SET result=? WHERE run_id=?',
                  (json.dumps({**legacy, 'number': 999}), run_id))
    with store.connect() as conn:
        assert pr_delivery.select_prs(conn, run_id, url) == []


def test_capture_selection_uses_this_answers_exact_paths_and_one_of_each_kind(workspace):
    app, _ = workspace
    run_id = app.state.store.create_run('Capture handoff', '', 'demo', [])['id']
    root = captures.directory(app.state.settings, run_id)
    root.mkdir(parents=True)
    for name, raw in [('first.png', PNG), ('second.png', PNG + b'second'),
                      ('flow.webm', WEBM), ('other.webm', WEBM + b'other')]:
        (root / name).write_bytes(raw)
    answer = ('[Video](/workspace/moyai-captures/flow.webm)\n'
              '![Result](moyai-captures/second.png)\n'
              '[Extra](moyai-captures/first.png) [Extra video](moyai-captures/other.webm)')
    selected = pr_delivery.select_captures(app.state.settings, run_id, answer)
    assert [capture.name for capture in selected] == ['flow.webm', 'second.png']
    assert [capture.sha256 for capture in selected] == [
        hashlib.sha256(WEBM).hexdigest(), hashlib.sha256(PNG + b'second').hexdigest()]
    assert pr_delivery.select_captures(app.state.settings, run_id, 'No demo in this answer.') == []
    for reference in [
        '/workspace/moyai-captures/first.png.extra',
        '/workspace/moyai-captures/first.png?download=true',
        '/workspace/moyai-captures/first.png/child',
        '/workspace/private/moyai-captures/first.png',
        '/workspace/moyai-captures/../first.png',
        'https://other.example/moyai-captures/first.png',
        'https://other.example/?file=/workspace/moyai-captures/first.png',
        'https://workspace.example/api/runs/' + 'f' * 32 + '/computer/captures/first.png',
    ]:
        assert pr_delivery.select_captures(app.state.settings, run_id, f'[Other]({reference})') == [], reference


def test_capture_selection_skips_symlinks_invalid_bytes_and_oversized_files(workspace, monkeypatch):
    app, _ = workspace
    run_id = app.state.store.create_run('Invalid captures', '', 'demo', [])['id']
    root = captures.directory(app.state.settings, run_id)
    root.mkdir(parents=True)
    (root / 'valid.png').write_bytes(PNG)
    (root / 'link.png').symlink_to(root / 'valid.png')
    (root / 'corrupt.png').write_bytes(b'<html>not an image</html>')
    (root / 'large.png').write_bytes(PNG + b'x' * 100)
    monkeypatch.setattr(captures, 'MAX_FILE', len(PNG))
    answer = '\n'.join(f'[Capture](moyai-captures/{name})' for name in
                       ['link.png', 'corrupt.png', 'large.png', 'missing.png', 'valid.png'])
    assert [capture.name for capture in pr_delivery.select_captures(app.state.settings, run_id, answer)] == ['valid.png']


async def test_entire_capture_batch_is_revalidated_before_any_external_upload(slack_app, monkeypatch):
    app, _, run_id = start(slack_app)
    channel = app.state.slack.channel
    source = channel.source_for_run(run_id, {'kind': 'answer'})
    credentials = await app.state.connectors.credentials('slack')
    credentials['bot']['scope'] += ',files:write'
    app.state.connectors.save('slack', credentials, 'Test organization')
    root = captures.directory(app.state.settings, run_id)
    root.mkdir(parents=True)
    (root / 'flow.webm').write_bytes(WEBM)
    (root / 'result.png').write_bytes(PNG)
    selected = pr_delivery.select_captures(app.state.settings, run_id,
        '[Video](moyai-captures/flow.webm) ![Image](moyai-captures/result.png)')
    assert [capture.name for capture in selected] == ['flow.webm', 'result.png']
    (root / 'result.png').write_bytes(PNG + b'replaced after answer collection')
    uploads = []

    async def request(method, url, **kwargs):
        if '/files.' in url:
            uploads.append(url)
            raise AssertionError('A changed batch reached Slack')
        return {'ok': True}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    assert await channel.upload_captures(source, [capture.model_dump() for capture in selected]) is False
    assert uploads == []


# Sidebar state is read from GitHub for confirmed receipts, never from answer text.
def sidebar_github(app, monkeypatch):
    from test_github import select
    from app.session_pull_requests import SessionPullRequests
    select(app, (202,))
    github = app.state.connectors.github
    github.references_dirty = False
    calls, responses = [], {}

    async def token(credentials, *, repository):
        assert repository == 202
        return 'read-token'

    async def request(method, path, **kwargs):
        calls.append((method, path))
        assert method == 'GET' and path.startswith('/repositories/202/pulls/')
        assert kwargs == {'token': 'read-token', 'missing': True}
        value = responses[int(path.rsplit('/', 1)[1])]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(github, 'installation_token', token)
    monkeypatch.setattr(github, 'request', request)
    return SessionPullRequests(github), calls, responses


def pr_response(number=100, **changes):
    from test_github import repository_data
    return {'number': number, 'html_url': f'https://github.com/BerriAI/moyai/pull/{number}',
            'title': 'A confirmed PR', 'state': 'open', 'draft': False, 'merged': False,
            'requested_reviewers': [], 'requested_teams': [],
            'base': {'repo': repository_data('BerriAI/moyai')}, **changes}


async def refreshed(service, run_ids):
    import asyncio
    service.summaries(run_ids)
    await asyncio.gather(*list(service.pending.values()))
    return service.summaries(run_ids)


async def test_sidebar_pr_family_deduplicates_receipts_and_never_counts_answer_links(workspace, monkeypatch):
    app, _ = workspace
    store = app.state.store
    parent, child, grandchild, deleted, other = [store.create_run('PR state', '', 'demo', [])['id'] for _ in range(5)]
    for identity, ancestor in [(child, parent), (grandchild, child), (deleted, parent)]:
        store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (ancestor, identity))
    store.execute("UPDATE runs SET deleted_at='deleted' WHERE id=?", (deleted,))
    for run_id, number in [(parent, 100), (child, 100), (child, 101), (grandchild, 102), (deleted, 103), (other, 104)]:
        publication(app, run_id, number)
    # Existing receipts without a repository ID use the established alias resolver.
    store.execute('UPDATE runs SET summary=? WHERE id=?', ('PR is ready https://github.com/BerriAI/moyai/pull/999', parent))
    service, calls, responses = sidebar_github(app, monkeypatch)
    responses.update({n: pr_response(n) for n in (100, 101, 102)})
    first = service.summaries([parent])[parent]
    assert first['unknown'] == 2 and first['label'] == ''
    result = await refreshed(service, [parent, child, deleted])
    assert result[parent]['open'] == 2 and result[parent]['label'] == 'PR is ready'
    assert {pr['number'] for pr in result[parent]['pull_requests']} == {100, 101}
    assert {pr['number'] for pr in result[child]['pull_requests']} == {100, 101, 102}
    assert result[deleted]['pull_requests'] == []
    assert len(calls) == 3
    await service.close()


async def test_sidebar_pr_states_refresh_and_partial_or_stale_facts_suppress_readiness(workspace, monkeypatch):
    from app.connector_errors import ConnectorError
    app, _ = workspace
    run_id = app.state.store.create_run('PR states', '', 'demo', [])['id']
    for number in range(100, 104):
        publication(app, run_id, number)
    service, calls, responses = sidebar_github(app, monkeypatch)
    responses.update({100: pr_response(), 101: pr_response(101, requested_teams=[{'id': 1}]),
                      102: pr_response(102, state='closed', merged=True), 103: pr_response(103, state='closed')})
    state = (await refreshed(service, [run_id]))[run_id]
    assert (state['open'], state['merged'], state['closed'], state['unknown'], state['label']) == (2, 1, 1, 0, 'Review PR')
    responses[100] = pr_response(state='closed', merged=True)
    responses[101] = ConnectorError('Temporary outage')
    for cached in service.cache.values():
        cached.fresh_until = cached.retry_at = 0
    pending = service.summaries([run_id])[run_id]
    assert pending['stale'] and not pending['label']
    state = (await refreshed(service, [run_id]))[run_id]
    assert (state['open'], state['merged'], state['closed']) == (1, 2, 1)
    assert state['stale'] and not state['label']
    assert len(calls) == 8  # A failed refresh does not retry on every sidebar poll.
    responses[101] = None  # Missing or inaccessible is unknown, never closed.
    for cached in service.cache.values():
        cached.retry_at = 0
    state = (await refreshed(service, [run_id]))[run_id]
    assert state['unknown'] == 1 and not state['label']
    responses[101] = pr_response(101, draft=True)
    for cached in service.cache.values():
        cached.retry_at = 0
    state = (await refreshed(service, [run_id]))[run_id]
    assert state['open'] == 1 and not state['label'] and not state['stale']
    await service.close()


async def test_sidebar_pr_rechecks_connection_policy_selection_and_inflight_rotation(workspace, monkeypatch):
    from test_github import select
    app, _ = workspace
    store = app.state.store
    run_id = store.create_run('PR access', '', 'demo', [])['id']
    publication(app, run_id)
    service, calls, responses = sidebar_github(app, monkeypatch)
    responses[100] = pr_response()
    assert (await refreshed(service, [run_id]))[run_id]['open'] == 1
    store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('github',0) ON CONFLICT(provider) DO UPDATE SET enabled=0")
    assert service.summaries([run_id])[run_id]['unknown'] == 1
    assert len(calls) == 1 and not service.pending
    store.execute("UPDATE connection_policies SET enabled=1,read_only=1 WHERE provider='github'")
    assert service.summaries([run_id])[run_id]['open'] == 1  # Read-only still allows status reads.
    select(app, (101,))
    assert service.summaries([run_id])[run_id]['unknown'] == 1
    assert not service.pending
    select(app, (202,))
    # Old authorization-key cache entries cannot publish into the new scope.
    assert service.summaries([run_id])[run_id]['unknown'] == 1
    saved = store.rows('SELECT * FROM github_pr_snapshots')
    async def rotate(method, path, **kwargs):
        select(app, (101,))
        return pr_response()
    monkeypatch.setattr(service.github, 'request', rotate)
    await refreshed(service, [run_id])
    assert service.summaries([run_id])[run_id]['unknown'] == 1
    assert store.rows('SELECT * FROM github_pr_snapshots') == saved
    await service.close()


async def test_sidebar_pr_refreshes_are_bounded_coalesced_and_cancellable(workspace, monkeypatch):
    import asyncio
    app, _ = workspace
    run_id = app.state.store.create_run('Bounded status', '', 'demo', [])['id']
    for number in range(100, 110):
        publication(app, run_id, number)
    service, _, _ = sidebar_github(app, monkeypatch)
    service.MAX_PENDING = 5
    started = []
    blocked = asyncio.Event()
    async def request(method, path, **kwargs):
        started.append(path)
        await blocked.wait()
        return pr_response(int(path.rsplit('/', 1)[1]))
    monkeypatch.setattr(service.github, 'request', request)
    for _ in range(10):
        service.summaries([run_id])
    assert len(service.pending) == 5
    await asyncio.sleep(0)
    assert len(started) == 4
    await service.close()
    assert not service.pending and not service.cache
    service.summaries([run_id])
    assert not service.pending
    # Cancellation before the coroutine's first step also releases its key.
    fresh, _, _ = sidebar_github(app, monkeypatch)
    fresh.summaries([run_id])
    for task in list(fresh.pending.values()):
        task.cancel()
    await asyncio.gather(*list(fresh.pending.values()), return_exceptions=True)
    assert not fresh.pending
    await fresh.close()


async def test_sidebar_pr_rejects_mismatched_or_incomplete_github_snapshots(workspace, monkeypatch):
    app, _ = workspace
    run_id = app.state.store.create_run('Validate status', '', 'demo', [])['id']
    publication(app, run_id)
    service, _, responses = sidebar_github(app, monkeypatch)
    malformed = [
        pr_response(number=999), pr_response(html_url='https://github.com.evil.example/BerriAI/moyai/pull/100'),
        pr_response(base={'repo': {'id': 999, 'full_name': 'BerriAI/moyai'}}),
        pr_response(merged='false'), pr_response(draft='false'), pr_response(state='open', merged=True),
        pr_response(requested_reviewers={}), pr_response(requested_teams=[{'id': False}]),
        {key: value for key, value in pr_response().items() if key != 'merged'},
        pr_response(created_at='2026-10-07'), pr_response(created_at=123),
        pr_response(created_at='0001-01-01T00:00:00+14:00'),
        pr_response(state='closed', merged=True, merged_at='2026-10-07T12:00:00'),
        pr_response(state='closed', merged=True, merged_at='not a date'),
        pr_response(merged_at='2026-10-07T12:00:00Z'),
        pr_response(state='closed', merged=True, created_at='2026-10-07T13:00:00Z',
                    merged_at='2026-10-07T12:00:00Z'),
    ]
    for response in malformed:
        responses[100] = response
        for cached in service.cache.values():
            cached.retry_at = 0
        summary = (await refreshed(service, [run_id]))[run_id]
        assert summary['unknown'] == 1 and not summary['label'], response
    await service.close()


async def test_pr_status_restores_verified_dates_stale_and_keeps_current_authorization(workspace, monkeypatch):
    from datetime import datetime
    from app.connector_errors import ConnectorError
    from app.db import Store
    from app.session_pull_requests import Receipt, SessionPullRequests
    from test_github import select
    app, _ = workspace
    store = app.state.store
    run_id = store.create_run('Retained PR status', '', 'demo', [])['id']
    publication(app, run_id)
    service, calls, responses = sidebar_github(app, monkeypatch)
    responses[100] = pr_response(state='closed', merged=True,
        created_at='2026-09-30T23:00:00-07:00', merged_at='2026-10-07T23:30:00-07:00')
    result = (await refreshed(service, [run_id]))[run_id]
    pr = result['pull_requests'][0]
    assert pr['created_at'] == '2026-10-01T06:00:00+00:00'
    assert pr['merged_at'] == '2026-10-08T06:30:00+00:00'
    receipt = Receipt.model_validate_json(store.rows('SELECT result FROM github_publications')[0]['result'])
    saved = store.rows('SELECT * FROM github_pr_snapshots')
    assert len(saved) == 1 and datetime.fromisoformat(saved[0]['observed_at']).utcoffset().total_seconds() == 0
    await service.close()

    # Open the same SQLite database through a new Store/service, as at startup.
    reopened = Store(store.path.parent)
    monkeypatch.setattr(service.github, 'store', reopened)
    restored = SessionPullRequests(service.github)
    target, key, cached = restored.status(receipt, restored.context())
    assert target == 202 and key == (service.github.connection_version(), 202, 100)
    assert cached.value.merged_at == pr['merged_at'] and cached.fresh_until == 0
    responses[100] = ConnectorError('Temporary GitHub outage')
    result = (await refreshed(restored, [run_id]))[run_id]
    assert result['merged'] == 1 and result['stale']
    assert result['pull_requests'][0]['merged_at'] == pr['merged_at']
    for _ in range(3):
        restored.summaries([run_id])
    assert len(calls) == 2 and not restored.pending
    assert reopened.rows('SELECT * FROM github_pr_snapshots') == saved

    reopened.execute("INSERT INTO connection_policies(provider,enabled) VALUES('github',0)")
    assert restored.status(receipt, restored.context()) == (None, None, None)
    reopened.execute("UPDATE connection_policies SET enabled=1 WHERE provider='github'")
    select(app, (101,))
    assert restored.summaries([run_id])[run_id]['unknown'] == 1
    select(app, (202,))
    assert restored.status(receipt, restored.context())[2] is None
    await restored.close()


async def test_pr_status_missing_observation_survives_restart_without_reviving_merge(workspace, monkeypatch):
    from app.session_pull_requests import Receipt, SessionPullRequests
    app, _ = workspace
    store = app.state.store
    run_id = store.create_run('Missing PR', '', 'demo', [])['id']
    publication(app, run_id)
    service, _, responses = sidebar_github(app, monkeypatch)
    responses[100] = pr_response(state='closed', merged=True, merged_at='2026-10-07T12:00:00Z')
    assert (await refreshed(service, [run_id]))[run_id]['merged'] == 1
    responses[100] = None
    for cached in service.cache.values():
        cached.retry_at = 0
    assert (await refreshed(service, [run_id]))[run_id]['unknown'] == 1
    assert store.rows('SELECT snapshot FROM github_pr_snapshots') == [{'snapshot': ''}]
    await service.close()
    receipt = Receipt.model_validate_json(store.rows('SELECT result FROM github_publications')[0]['result'])
    restored = SessionPullRequests(service.github)
    assert restored.status(receipt, restored.context())[2].value is None
    assert (await refreshed(restored, [run_id]))[run_id]['unknown'] == 1
    await restored.close()

    # Older merged records may lack timestamps; that does not invent a date.
    restored = SessionPullRequests(service.github)
    responses[100] = pr_response(state='closed', merged=True)
    result = (await refreshed(restored, [run_id]))[run_id]
    assert result['merged'] == 1
    assert result['pull_requests'][0]['merged_at'] is None
    await restored.close()
    store.execute("UPDATE github_pr_snapshots SET snapshot=?", ('{"state":"merged"}',))
    restored = SessionPullRequests(service.github)
    assert restored.status(receipt, restored.context())[2] is None
    await restored.close()


async def test_sidebar_pr_lookup_batches_large_pinned_lists_and_tolerates_bad_connection(workspace, monkeypatch):
    app, _ = workspace
    store = app.state.store
    run_id = store.create_run('Large sidebar', '', 'demo', [])['id']
    publication(app, run_id)
    service, _, responses = sidebar_github(app, monkeypatch)
    responses[100] = pr_response()
    original = store.rows
    sizes = []
    def rows(query, parameters=()):
        if 'FROM github_publications p' in query:
            sizes.append(len(parameters))
            assert len(parameters) <= 800
        return original(query, parameters)
    monkeypatch.setattr(store, 'rows', rows)
    selected = [str(number) for number in range(1000)] + [run_id]
    assert (await refreshed(service, selected))[run_id]['open'] == 1
    assert len(sizes) >= 3
    for config in ['not JSON', '[]', '{"kind":"github_app","repository_ids":[202]}']:
        store.execute("UPDATE connections SET encrypted=? WHERE provider='github'", (app.state.security.encrypt(config),))
        assert service.summaries([run_id])[run_id]['unknown'] == 1
    store.execute("UPDATE connections SET encrypted='broken encryption' WHERE provider='github'")
    assert service.summaries([run_id])[run_id]['unknown'] == 1
    await service.close()


async def test_sidebar_pr_cache_capacity_preserves_retry_cooldowns(workspace, monkeypatch):
    from app.connector_errors import ConnectorError
    app, _ = workspace
    run_id = app.state.store.create_run('Cache pressure', '', 'demo', [])['id']
    for number in range(100, 106):
        publication(app, run_id, number)
    service, calls, responses = sidebar_github(app, monkeypatch)
    service.MAX_CACHE = 3
    responses.update({number: ConnectorError('Retry later') for number in range(100, 106)})
    await refreshed(service, [run_id])
    for _ in range(4):
        await refreshed(service, [run_id])
    assert len(calls) == 3 and len(service.cache) == 3 and not service.pending
    await service.close()


async def test_sidebar_pr_capacity_rotates_refreshes_across_large_lists(workspace, monkeypatch):
    app, _ = workspace
    run_id = app.state.store.create_run('Fair status refresh', '', 'demo', [])['id']
    for number in range(100, 106):
        publication(app, run_id, number)
    service, calls, responses = sidebar_github(app, monkeypatch)
    service.MAX_PENDING, service.MAX_CACHE = 2, 3
    responses.update({number: pr_response(number) for number in range(100, 106)})
    for _ in range(8):
        await refreshed(service, [run_id])
        for cached in service.cache.values():
            cached.retry_at = cached.fresh_until = 0
    assert {path for _, path in calls} == {f'/repositories/202/pulls/{number}' for number in range(100, 106)}
    assert len(service.cache) <= 3
    await service.close()


async def test_sidebar_pr_mixed_legacy_receipts_share_identity_when_alias_is_missing(workspace, monkeypatch):
    from test_github import repository_data
    app, _ = workspace
    store = app.state.store
    parent, child = [store.create_run('Mixed PR receipts', '', 'demo', [])['id'] for _ in range(2)]
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent, child))
    for run_id in (parent, child):
        publication(app, run_id)
    saved = json.loads(store.rows('SELECT result FROM github_publications WHERE run_id=?', (child,))[0]['result'])
    store.execute('UPDATE github_publications SET result=? WHERE run_id=?', (json.dumps({**saved, 'repository_id': 202}), child))
    service, calls, responses = sidebar_github(app, monkeypatch)
    renamed = 'BerriAI/moyai-renamed'
    store.execute('UPDATE github_repositories SET full_name=?,aliases=? WHERE repository_id=202',
                  (renamed, json.dumps([renamed.lower()])))
    responses[100] = pr_response(html_url=f'https://github.com/{renamed}/pull/100',
        base={'repo': {**repository_data('BerriAI/moyai'), 'full_name': renamed}})
    # A current ID-backed receipt identifies the same saved canonical URL as the
    # legacy receipt, even though the live repository only knows its new name.
    summary = (await refreshed(service, [parent]))[parent]
    assert (summary['open'], summary['unknown'], summary['label']) == (1, 0, 'PR is ready')
    assert len(summary['pull_requests']) == 1 and summary['pull_requests'][0]['url'].endswith('/moyai-renamed/pull/100')
    assert calls == [('GET', '/repositories/202/pulls/100')]
    # Neither receipt ordering nor disabling the connection splits that identity.
    store.execute("UPDATE github_publications SET created_at='0000' WHERE run_id=?", (child,))
    assert service.summaries([parent])[parent]['open'] == 1
    store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('github',0) ON CONFLICT(provider) DO UPDATE SET enabled=0")
    summary = service.summaries([parent])[parent]
    assert summary['unknown'] == 1 and not summary['label'] and len(calls) == 1
    await service.close()


async def test_sidebar_pr_legacy_identity_does_not_join_conflicting_ids_or_other_sessions(workspace, monkeypatch):
    from test_github import repository_data
    app, _ = workspace
    store = app.state.store
    legacy, identified, conflicting = [store.create_run('PR identity scope', '', 'demo', [])['id'] for _ in range(3)]
    for run_id, repository_id in [(legacy, None), (identified, 202), (conflicting, 999)]:
        publication(app, run_id)
        if repository_id:
            row = store.rows('SELECT result FROM github_publications WHERE run_id=?', (run_id,))[0]
            store.execute('UPDATE github_publications SET result=? WHERE run_id=?',
                          (json.dumps({**json.loads(row['result']), 'repository_id': repository_id}), run_id))
    service, calls, responses = sidebar_github(app, monkeypatch)
    renamed = 'BerriAI/moyai-renamed'
    store.execute('UPDATE github_repositories SET full_name=?,aliases=? WHERE repository_id=202',
                  (renamed, json.dumps([renamed.lower()])))
    responses[100] = pr_response(html_url=f'https://github.com/{renamed}/pull/100',
        base={'repo': {**repository_data('BerriAI/moyai'), 'full_name': renamed}})
    summaries = await refreshed(service, [legacy, identified])
    assert summaries[legacy]['unknown'] == 1 and not summaries[legacy]['label']
    assert summaries[identified]['open'] == 1
    # A reused repository name can refer to two permanent IDs. With no alias
    # resolution, the legacy receipt must remain unknown instead of picking one.
    store.execute('UPDATE runs SET parent_run_id=? WHERE id IN (?,?)', (legacy, identified, conflicting))
    summary = service.summaries([legacy])[legacy]
    assert (summary['open'], summary['unknown'], summary['label']) == (1, 2, '')
    assert len(summary['pull_requests']) == 3
    assert calls == [('GET', '/repositories/202/pulls/100')] and not service.pending
    await service.close()
