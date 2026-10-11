"""Bulk metadata refresh preserves the selected IDs without per-repo code tokens."""
import asyncio

import pytest

from app.connector_errors import ConnectorError
from test_github import credentials, repository_data, select
from test_github_identity import identity  # noqa: F401
from test_workspace import workspace  # noqa: F401


@pytest.mark.parametrize('count,expected_calls', [(15, 3), (100, 3), (101, 4), (500, 7), (501, 9)])
def test_refresh_batches_selected_metadata_with_bounded_request_count(identity, count, expected_calls):
    app, client, run, headers, github, provider = identity
    ids = list(range(1000, 1000 + count))
    for i in ids:
        provider.repos[i] = {**repository_data('BerriAI/litellm'), 'id': i, 'full_name': f'BerriAI/repo-{i}'}
    provider.installed.extend(ids)
    app.state.connectors.save('github', credentials(ids), 'Selected repositories')
    version = github.connection_version()
    result = client.post(f'/broker/{run}/tools/call', headers=headers,
                         json={'name': 'github_repositories', 'arguments': {}}).json()
    assert result == {'repositories': [{'id': i, 'full_name': f'BerriAI/repo-{i}'} for i in ids]}
    assert len(provider.calls) == expected_calls
    bodies = [body for method, path, body in provider.calls if method == 'POST']
    assert all(body['permissions'] == {'metadata': 'read'} for body in bodies)
    assert [i for body in bodies for i in body['repository_ids']] == ids
    assert all(len(body['repository_ids']) <= 500 for body in bodies)
    assert github.saved_credentials()['repository_ids'] == ids
    assert github.connection_version() == version
    assert not github.tokens  # Listing neither mints nor warms code/PR tokens.


def test_refresh_tracks_rename_without_selecting_other_installed_repositories(identity):
    app, _, run, _, github, provider = identity
    app.state.store.execute('UPDATE runs SET github_repository_id=101,repo_url=? WHERE id=?',
                            ('https://github.com/BerriAI/litellm', run))
    provider.repos[101]['full_name'] = 'BerriAI/renamed'
    asyncio.run(github.refresh_connection())
    assert github.repository_options() == [{'id': 101, 'full_name': 'BerriAI/renamed'}]
    assert github.saved_credentials()['repository_ids'] == [101]
    assert app.state.store.run(run)['repo_url'] == 'https://github.com/BerriAI/renamed'
    with pytest.raises(ConnectorError, match='not selected'):
        github.target(202)


@pytest.mark.parametrize('bad', ['removed', 'missing', 'extra', 'duplicate', 'transferred'])
def test_refresh_rejects_revocation_or_changed_identity(identity, bad):
    _, _, _, _, github, provider = identity
    if bad == 'removed':
        provider.installed.remove(101)
    elif bad == 'transferred':
        provider.repos[101]['owner'] = {'id': 999}
    else:
        provider.metadata_pages = {'missing': [[]], 'extra': [[provider.repos[101], provider.repos[202]]],
                                   'duplicate': [[provider.repos[101], provider.repos[101]]]}[bad]
    with pytest.raises(ConnectorError):
        asyncio.run(github.refresh_connection())
    assert github.saved_credentials()['repository_ids'] == [101]


@pytest.mark.parametrize('change', ['disconnect', 'reconnect', 'key'])
def test_connection_changes_win_over_inflight_refresh(identity, change):
    app, _, _, _, github, provider = identity
    provider.repos[101]['full_name'] = 'BerriAI/renamed'
    def mutate(method, path):
        if path != '/installation/repositories':
            return
        if change == 'disconnect':
            app.state.store.execute("DELETE FROM connections WHERE provider='github'")
        elif change == 'reconnect':
            select(app, (202,))
        else:
            github.save_app({**github.app_config(), 'pem': 'rotated-fixture-key'})
    provider.on_request = mutate
    with pytest.raises(ConnectorError, match='changed'):
        asyncio.run(github.refresh_connection())
    rows = app.state.store.rows('SELECT full_name FROM github_repositories WHERE repository_id=101')
    assert rows[0]['full_name'] == 'BerriAI/litellm'
    if change == 'disconnect':
        assert github.saved_credentials() == {}
    elif change == 'reconnect':
        assert github.saved_credentials()['repository_ids'] == [202]
