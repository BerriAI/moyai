"""Exercise workspace publication access, concurrency and response recovery."""
import asyncio
import copy
import json
import hashlib

import httpx
import pytest
from pydantic import ValidationError

from app.connector_errors import ConnectorError
from app.github import Change, Comment, GitHub, Publish, Rebase, Update
from app.github_publications import REBASE_REF_MUTATION
from app.security import digest
from sandbox.broker_transport import CONTENT_TYPE, seal, unseal, MAX_BODY
from test_github import BASE, CURRENT, COMMIT, TREE, PAYLOAD, GitHubAPI, connected
from test_workspace import workspace

NEXT = '1' * 40
REBASE_TREE = '2' * 40


class FollowupAPI(GitHubAPI):
    def __init__(self, *args):
        super().__init__(*args)
        self.comments = []
        self.next_commit = NEXT

    async def request(self, method, path, **kwargs):
        prefix = '/repositories/' + str(self.repository_id)
        if path in {prefix + '/pulls/100', prefix + '/git/commits/' + COMMIT} or (
                path.endswith('/comments') or path.endswith('/reviews') or method == 'PATCH'
                or (method == 'POST' and path.endswith('/git/commits') and self.pr)):
            self.calls.append((method, path, kwargs))
            if self.on_call:
                self.on_call(method, path)
            if path.endswith('/pulls/100'):
                return {**self.pr, 'head': {**self.pr['head'], 'sha': self.branch}}
            if path.endswith('/git/commits/' + COMMIT):
                return {'tree': {'sha': TREE}}
            if path.endswith('/git/commits'):
                return {'sha': self.next_commit}
            if method == 'PATCH':
                assert '/git/refs/heads/' in path and kwargs['json']['force'] is False
                self.branch = kwargs['json']['sha']
                self.named_branches[path.split('/git/refs/heads/', 1)[1]] = self.branch
                if self.lose == 'update':
                    self.lose = ''
                    raise ConnectorError('Lost response')
                return {}
            if method == 'GET':
                return copy.deepcopy(self.comments)
            comment = {'id': 321, 'html_url': self.pr['html_url'] + '#issuecomment-321', **kwargs['json']}
            self.comments.append(comment)
            if self.lose == 'comment':
                self.lose = ''
                raise ConnectorError('Lost response')
            return comment
        return await super().request(method, path, **kwargs)


class RebaseAPI(FollowupAPI):
    async def request(self, method, path, **kwargs):
        if method == 'GET' and path.endswith('/git/commits/' + CURRENT):
            self.calls.append((method, path, kwargs))
            return {'sha': CURRENT, 'tree': {'sha': REBASE_TREE}}
        if path == '/graphql' and kwargs.get('json', {}).get('query') == REBASE_REF_MUTATION:
            self.calls.append((method, path, kwargs))
            if self.on_call:
                self.on_call(method, path)
            variables = kwargs['json']['variables']
            assert variables['repositoryId'] == f'R_{self.repository_id}'
            assert variables['name'] == 'refs/heads/' + self.pr['head']['ref']
            if self.branch != variables['beforeOid']:
                return {'data': {'updateRefs': None}, 'errors': [{'message': 'beforeOid does not match'}]}
            self.branch = variables['afterOid']
            self.named_branches[self.pr['head']['ref']] = self.branch
            if self.lose == 'rebase':
                self.lose = ''
                raise ConnectorError('Lost atomic rebase response')
            return {'data': {'updateRefs': {'clientMutationId': None}}}
        return await super().request(method, path, **kwargs)


@pytest.fixture
def rebasing(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = RebaseAPI(github, monkeypatch)
    run = app.state.store.run(run_id)
    receipt = asyncio.run(github.publish(run, Publish.model_validate(PAYLOAD)))
    api.pr['base']['sha'] = CURRENT
    api.tree = [{'path': 'CODEOWNERS', 'type': 'blob', 'mode': '100644', 'sha': '3' * 40}]
    api.calls.clear()
    return app, client, run, headers, github, api, receipt


def rebase_args():
    return Rebase(repository_id=101, number=100, base_sha=CURRENT, expected_head_sha=COMMIT,
                  title='Rebase prepared changes', files=PAYLOAD['files'], request_key='rebase-prepared-123')


@pytest.mark.parametrize('empty', [False, True])
def test_prepared_rebase_replaces_one_head_and_preserves_pr_provenance(rebasing, empty):
    app, client, run, headers, github, api, original = rebasing
    publications = app.state.store.rows('SELECT * FROM github_publications')
    args = rebase_args()
    if empty:
        args = Rebase.model_validate({**args.model_dump(), 'files': []})
    result = client.post(f"/broker/{run['id']}/tools/call", headers=headers,
                        json={'name': 'github_rebase_pull_request', 'arguments': args.model_dump()}).json()
    assert result['number'] == original['number'] == 100
    assert result['commit'] == NEXT and result['base_sha'] == CURRENT and result['previous_head_sha'] == COMMIT
    assert result['rebased'] and result['operation'] == 'prepared_single_commit_rebase'
    assert app.state.store.rows('SELECT * FROM github_publications') == publications
    assert len(app.state.store.rows("SELECT * FROM events WHERE json_text(data,'phase')='pr_created'")) == 1
    commit = next(call[2]['json'] for call in api.calls if call[1].endswith('/git/commits'))
    assert commit['parents'] == [CURRENT] and COMMIT not in commit['parents']
    trees = [call[2]['json'] for call in api.calls if call[0] == 'POST' and call[1].endswith('/git/trees')]
    if empty:
        assert not trees and commit['tree'] == REBASE_TREE
    else:
        assert trees[0]['base_tree'] == REBASE_TREE
        assert [entry['path'] for entry in trees[0]['tree']] == ['fix.py']
    atomic = [call for call in api.calls if call[1] == '/graphql' and call[2]['json']['query'] == REBASE_REF_MUTATION]
    assert len(atomic) == 1 and atomic[0][2]['json']['variables'] == {
        'repositoryId': 'R_101', 'name': 'refs/heads/' + original['branch'], 'beforeOid': COMMIT, 'afterOid': NEXT}
    assert not any(method == 'PATCH' or method == 'POST' and path.endswith('/pulls') for method, path, _ in api.calls)
    api.calls.clear()
    assert asyncio.run(github.rebase_pull_request(run, args)) == result
    assert not api.mutations


def test_rebase_accepts_branch_provenance_without_inventing_pr_authorship(workspace, monkeypatch):
    from app.github import CreateBranch
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = RebaseAPI(github, monkeypatch)
    run = app.state.store.run(run_id)
    branch = 'moyai/branch-only'
    asyncio.run(github.controlled_publication(run, CreateBranch(repository_id=101, branch=branch,
        source_sha=BASE, request_key='owned-branch-only'), 'github_create_branch'))
    api.pr = {'number': 100, 'state': 'open', 'draft': False, 'merged': False,
              'html_url': 'https://github.com/BerriAI/litellm/pull/100',
              'head': {'ref': branch, 'sha': BASE, 'repo': {'id': 101}},
              'base': {'ref': 'main', 'sha': CURRENT, 'repo': {'id': 101}}}
    publications = app.state.store.rows('SELECT * FROM github_publications')
    assert json.loads(publications[0]['result'])['kind'] == 'branch'
    args = Rebase.model_validate({**rebase_args().model_dump(), 'expected_head_sha': BASE})
    result = asyncio.run(github.rebase_pull_request(run, args))
    assert result['number'] == 100 and result['commit'] == NEXT and result['rebased']
    assert app.state.store.rows('SELECT * FROM github_publications') == publications
    with pytest.raises(ConnectorError):
        github.owned_publication(101, 100, github.connection_version())
    api.calls.clear()
    with pytest.raises(ConnectorError):
        asyncio.run(github.comment(run, comment_args()))
    assert not api.mutations


def test_rebase_cannot_trust_an_owned_marker_without_creation_ack(rebasing):
    app, _, run, _, github, api, original = rebasing
    publication = app.state.store.rows('SELECT id FROM github_publications')[0]
    app.state.store.execute("UPDATE github_publication_effects SET response='' WHERE publication_id=? AND effect='branch'",
                            (publication['id'],))
    assert not github.branch_owned(101, original['branch'], github.connection_version())
    with pytest.raises(ConnectorError):
        asyncio.run(github.rebase_pull_request(run, rebase_args()))
    assert not api.mutations


@pytest.mark.parametrize('boundary', ['old_base', 'stale_head', 'retarget', 'foreign_head', 'default', 'protected',
                                     'read_only', 'reconnect', 'unselected', 'missing_provenance'])
def test_rebase_boundaries_reject_before_any_mutation(rebasing, boundary):
    app, _, run, _, github, api, original = rebasing
    args = rebase_args()
    if boundary == 'old_base':
        args = args.model_copy(update={'base_sha': BASE})
    elif boundary == 'stale_head':
        api.branch = BASE
    elif boundary == 'retarget':
        api.pr['base']['sha'] = BASE
    elif boundary == 'foreign_head':
        api.pr['head']['ref'] = 'human/other'
    elif boundary == 'default':
        api.default_branch = original['branch']
    elif boundary == 'protected':
        api.protected.add(original['branch'])
    elif boundary == 'read_only':
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    elif boundary == 'reconnect':
        github.save_app({'id': 456, 'pem': 'replacement'})
    elif boundary == 'unselected':
        args = args.model_copy(update={'repository_id': 999})
    elif boundary == 'missing_provenance':
        app.state.store.execute('DELETE FROM github_owned_branches')
    with pytest.raises(ConnectorError):
        asyncio.run(github.rebase_pull_request(run, args))
    assert not api.mutations


@pytest.mark.parametrize('point', ['before_dispatch', 'at_dispatch'])
def test_rebase_stale_head_is_never_forced_past_expected_oid(rebasing, point):
    _, _, run, _, github, api, _ = rebasing
    def race(method, path):
        if point == 'before_dispatch' and method == 'POST' and path.endswith('/git/commits'):
            api.branch = BASE
        if point == 'at_dispatch' and method == 'POST' and path == '/graphql':
            api.branch = BASE
    api.on_call = race
    for _ in range(2):
        with pytest.raises(ConnectorError):
            asyncio.run(github.rebase_pull_request(run, rebase_args()))
    assert api.branch == BASE
    assert len([call for call in api.calls if call[1] == '/graphql']) == (1 if point == 'at_dispatch' else 0)


@pytest.mark.parametrize('accepted', [False, True])
def test_rebase_restart_only_reconciles_claimed_atomic_write(rebasing, monkeypatch, accepted):
    app, _, run, _, github, api, _ = rebasing
    attempts = []
    async def request(method, path, **kwargs):
        if path == '/graphql':
            attempts.append(path)
            if not accepted:
                raise ConnectorError('Lost atomic response before readback')
        return await api.request(method, path, **kwargs)
    monkeypatch.setattr(github, 'request', request)
    if accepted:
        api.lose = 'rebase'
    with pytest.raises(ConnectorError, match='Lost'):
        asyncio.run(github.rebase_pull_request(run, rebase_args()))
    restored = GitHub(github.store, github.security, github.settings, github.connectors)
    monkeypatch.setattr(restored, 'request', request)
    monkeypatch.setattr(restored, 'installation_token', github.installation_token)
    app.state.store.execute('UPDATE runs SET active_message_id=42 WHERE id=?', (run['id'],))
    current_run = app.state.store.run(run['id'])
    if accepted:
        result = asyncio.run(restored.rebase_pull_request(current_run, rebase_args()))
        assert result['commit'] == NEXT and result['rebased']
    else:
        with pytest.raises(ConnectorError):
            asyncio.run(restored.rebase_pull_request(current_run, rebase_args()))
    assert len(attempts) == 1


@pytest.mark.parametrize('response', [None, {}, {'data': {'updateRefs': None}},
                                     {'errors': [{'message': 'partial failure'}]}])
def test_malformed_rebase_ack_is_only_recovered_by_reads(rebasing, monkeypatch, response):
    _, _, run, _, github, api, _ = rebasing
    async def request(method, path, **kwargs):
        result = await api.request(method, path, **kwargs)
        return response if path == '/graphql' else result
    monkeypatch.setattr(github, 'request', request)
    with pytest.raises(ConnectorError, match='did not confirm'):
        asyncio.run(github.rebase_pull_request(run, rebase_args()))
    assert asyncio.run(github.rebase_pull_request(run, rebase_args()))['commit'] == NEXT
    assert len([call for call in api.calls if call[1] == '/graphql']) == 1


@pytest.mark.parametrize('change', ['base', 'protection', 'requester', 'revoke'])
def test_rebase_rechecks_authority_and_target_after_preparation(rebasing, change):
    app, _, run, _, github, api, original = rebasing
    def changed(method, path):
        if method != 'POST' or not path.endswith('/git/commits'):
            return
        if change == 'base':
            api.pr['base']['sha'] = BASE
            api.named_branches['main'] = BASE
        elif change == 'protection':
            api.protected.add(original['branch'])
        elif change == 'requester':
            app.state.store.execute("UPDATE runs SET active_user_id='other-requester' WHERE id=?", (run['id'],))
        else:
            app.state.store.update_run(run['id'], status='cancelled')
    api.on_call = changed
    with pytest.raises(ConnectorError):
        asyncio.run(github.rebase_pull_request(run, rebase_args()))
    assert not any(path == '/graphql' for _, path, _ in api.calls)
    assert api.branch == COMMIT


def test_rebase_workflow_edits_retain_dedicated_token_requirement(rebasing, monkeypatch):
    _, _, run, _, github, api, _ = rebasing
    original = github.installation_token
    requested = []
    async def token(*args, **kwargs):
        requested.append(kwargs)
        if kwargs.get('workflows'):
            raise ConnectorError('Workflows write permission is required.')
        return await original(*args, **kwargs)
    monkeypatch.setattr(github, 'installation_token', token)
    args = Rebase.model_validate({**rebase_args().model_dump(),
        'files': [{'path': '.github/workflows/ci.yml', 'content': 'name: trusted change'}]})
    with pytest.raises(ConnectorError, match='Workflows write'):
        asyncio.run(github.rebase_pull_request(run, args))
    assert any(request.get('workflows') and request['repository'] == 101 for request in requested)
    assert not api.mutations


@pytest.mark.parametrize('source', [{'sha': BASE, 'tree': {'sha': TREE}},
                                  {'sha': CURRENT, 'tree': {'sha': '0' * 40}},
                                  {'tree': {'sha': REBASE_TREE}}])
def test_rebase_requires_verified_target_base_tree(rebasing, monkeypatch, source):
    _, _, run, _, github, api, _ = rebasing
    async def request(method, path, **kwargs):
        if method == 'GET' and path.endswith('/git/commits/' + CURRENT):
            return source
        return await api.request(method, path, **kwargs)
    monkeypatch.setattr(github, 'request', request)
    with pytest.raises(ConnectorError, match='exact rebase source'):
        asyncio.run(github.rebase_pull_request(run, rebase_args()))
    assert not api.mutations


def test_fixed_rebase_egress_requires_nonzero_exact_oids(workspace, monkeypatch):
    app, _ = workspace
    github = app.state.connectors.github
    real_client = httpx.AsyncClient
    observed = []
    def transport(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={'data': {'updateRefs': {'clientMutationId': None}}})
    monkeypatch.setattr('app.github.httpx.AsyncClient',
                        lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))
    variables = {'repositoryId': 'R_101', 'name': 'refs/heads/feature/rebase', 'beforeOid': COMMIT, 'afterOid': NEXT}
    response = asyncio.run(github.request('POST', '/graphql', token='fixture',
        json={'query': REBASE_REF_MUTATION, 'variables': variables}))
    assert response['data']['updateRefs'] == {'clientMutationId': None}
    for changed in ({'beforeOid': '0' * 40}, {'afterOid': '0' * 40}, {'beforeOid': None},
                    {'name': 'refs/tags/rebase'}, {'force': True}, {'repositoryId': ''}):
        with pytest.raises(ConnectorError):
            asyncio.run(github.request('POST', '/graphql', token='fixture',
                json={'query': REBASE_REF_MUTATION, 'variables': {**variables, **changed}}))
    assert len(observed) == 1


def test_two_brokers_claim_atomic_rebase_once(rebasing, monkeypatch):
    _, _, run, _, github, api, _ = rebasing
    second = GitHub(github.store, github.security, github.settings, github.connectors)
    monkeypatch.setattr(second, 'installation_token', github.installation_token)
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def request(method, path, **kwargs):
            if path == '/graphql':
                entered.set()
                await release.wait()
            return await api.request(method, path, **kwargs)
        monkeypatch.setattr(github, 'request', request)
        monkeypatch.setattr(second, 'request', request)
        first = asyncio.create_task(github.rebase_pull_request(run, rebase_args()))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with pytest.raises(ConnectorError):
                await asyncio.wait_for(second.rebase_pull_request(run, rebase_args()), 5)
        finally:
            release.set()
        result = await first
        assert await second.rebase_pull_request(run, rebase_args()) == result
    asyncio.run(exercise())
    assert len([call for call in api.calls if call[1] == '/graphql']) == 1


@pytest.mark.parametrize('change', [
    {'base_sha': '0' * 40}, {'expected_head_sha': '0' * 40}, {'force': True},
    {'files': [{'path': 'CODEOWNERS', 'content': 'unapproved'}]},
    {'files': [{'path': '.git/config', 'content': 'unapproved'}]},
])
def test_rebase_schema_never_exposes_unfenced_force_or_protected_edits(change):
    with pytest.raises(ValidationError):
        Rebase.model_validate({**rebase_args().model_dump(), **change})

@pytest.fixture
def published(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = FollowupAPI(github, monkeypatch)
    run = app.state.store.run(run_id)
    receipt = asyncio.run(github.publish(run, Publish.model_validate(PAYLOAD)))
    api.pr.update(head={'ref': receipt['branch'], 'sha': COMMIT, 'repo': {'id': 101, 'full_name': api.repository}})
    api.calls.clear()
    return app, client, run, headers, github, api


def update_args():
    return Update.model_validate({k: v for k, v in {
        **PAYLOAD, 'base_sha': COMMIT, 'number': 100, 'request_key': 'revision-two',
        'files': [{'path': 'fix.py', 'content': 'answer = 43\n'}]}.items() if k != 'body'})


def comment_args():
    return Comment(repository=PAYLOAD['repository'], number=100, body='@review-bot review', request_key='review-second-head')


def test_uncertain_comment_is_found_beyond_one_hundred_pages(published, monkeypatch):
    _, _, run, _, github, api = published
    api.lose = 'comment'
    args = comment_args()
    with pytest.raises(ConnectorError, match='Lost'):
        asyncio.run(github.comment(run, args))
    original = api.request
    pages = []
    async def paged(method, path, **kwargs):
        if method == 'GET' and path.endswith('/comments'):
            page = kwargs['params']['page']
            pages.append(page)
            if page <= 100:
                return [{'body': f'Unrelated comment on page {page}'}] * 100
        return await original(method, path, **kwargs)
    monkeypatch.setattr(github, 'request', paged)
    result = asyncio.run(github.comment(run, args))
    assert result['id'] == 321 and pages == list(range(1, 102))
    assert sum(method == 'POST' and path.endswith('/comments') for method, path, _ in api.calls) == 1


def another_session(app):
    run = app.state.store.create_run('Continue an existing PR', '', 'modal', ['github'])
    token = 'another-run-capability'
    app.state.store.update_run(run['id'], status='running', token_hash=digest(token))
    return app.state.store.run(run['id']), {'Authorization': 'Bearer ' + token}


@pytest.mark.parametrize('operation', ['update', 'comment'])
@pytest.mark.parametrize('lost', [False, True])
@pytest.mark.parametrize('new_chat', [False, True])
def test_followup_recovery_does_not_repeat_write_across_turns(published, monkeypatch, operation, lost, new_chat):
    app, client, run, headers, github, api = published
    publications = app.state.store.rows('SELECT * FROM github_publications')
    if new_chat:
        run, headers = another_session(app)
    url = f"/broker/{run['id']}/tools/call"
    checkout = client.post(url, headers=headers, json={'name': 'github_checkout',
        'arguments': {'repository_id': 101, 'number': 100}}).json()
    assert checkout['base_sha'] == COMMIT and checkout['checkout_ref'] == 'refs/pull/100/head'
    args = update_args() if operation == 'update' else comment_args()
    body = {'name': 'github_' + operation + '_pull_request', 'arguments': args.model_dump()}
    if lost:
        api.lose = operation
    first = client.post(url, headers=headers, json=body).json()
    assert ('Lost' in first.get('error', '')) if lost else 'url' in first
    # A new broker instance and turn recover the same durable operation.
    restored = GitHub(github.store, github.security, github.settings, github.connectors)
    monkeypatch.setattr(restored, 'request', api.request)
    monkeypatch.setattr(restored, 'installation_token', github.installation_token)
    monkeypatch.setattr(app.state.connectors, 'github', restored)
    app.state.store.execute('UPDATE runs SET active_message_id=42 WHERE id=?', (run['id'],))
    result = client.post(url, headers=headers, json=body).json()
    assert result['number'] == 100
    assert client.post(url, headers=headers, json=body).json() == result
    assert app.state.store.rows('SELECT * FROM github_publications') == publications
    assert not app.state.store.approvals(run['id'])
    writes = [c for c in api.calls if c[0] == ('PATCH' if operation == 'update' else 'POST')
              and (operation == 'update' or c[1].endswith('/comments'))]
    assert len(writes) == 1
    assert result.get('commit', NEXT) == NEXT
    assert len(api.comments) == (operation == 'comment')
    changed = args.model_copy(update={'title': 'Changed revision'} if operation == 'update' else {'body': 'Different request'})
    conflict = client.post(url, headers=headers, json={**body, 'arguments': changed.model_dump()}).json()
    assert 'different changes' in conflict['error']


@pytest.mark.parametrize('operation', ['update', 'comment'])
@pytest.mark.parametrize('new_chat', [False, True])
@pytest.mark.parametrize('restriction', ['missing', 'unconfirmed', 'malformed', 'non_object', 'foreign_connection',
    'foreign_repo', 'other_pr', 'other_repo', 'closed', 'merged', 'foreign_branch', 'fork', 'read_only', 'revoked', 'installation'])
def test_followup_boundaries_prevent_writes(published, operation, restriction, new_chat):
    app, client, run, headers, github, api = published
    if new_chat:
        run, headers = another_session(app)
    args = update_args() if operation == 'update' else comment_args()
    if restriction == 'missing':
        app.state.store.execute('DELETE FROM github_publications')
    elif restriction in {'unconfirmed', 'malformed', 'non_object'}:
        value = {'unconfirmed': '', 'malformed': '{', 'non_object': '[]'}[restriction]
        app.state.store.execute('UPDATE github_publications SET result=?', (value,))
    elif restriction == 'foreign_connection':
        app.state.store.execute("UPDATE github_publications SET connection_version='another-connection'")
    elif restriction == 'foreign_repo':
        receipt = json.loads(app.state.store.rows('SELECT result FROM github_publications')[0]['result'])
        app.state.store.execute('UPDATE github_publications SET result=?', (json.dumps({**receipt, 'repository_id': 202}),))
    elif restriction == 'other_pr':
        args = args.model_copy(update={'number': 101})
    elif restriction == 'other_repo':
        args = args.model_copy(update={'repository': 'BerriAI/another'})
    elif restriction == 'closed':
        api.pr['state'] = 'closed'
    elif restriction == 'merged':
        api.pr['merged'] = True
    elif restriction == 'foreign_branch':
        api.pr['head']['ref'] = 'main'
    elif restriction == 'fork':
        api.pr['head']['repo']['id'] = 999
    elif restriction == 'read_only':
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    elif restriction == 'revoked':
        app.state.store.update_run(run['id'], token_hash='')
    else:
        github.save_app({'id': 999, 'pem': 'different'})
    with pytest.raises(ConnectorError):
        asyncio.run(getattr(github, operation)(run, args))
    assert all(c[0] == 'GET' for c in api.calls)


def test_same_request_key_in_another_chat_does_not_replay_or_overwrite(published):
    app, client, run, headers, github, api = published
    second, second_headers = another_session(app)
    body = {'name': 'github_update_pull_request', 'arguments': update_args().model_dump()}
    first = client.post(f"/broker/{run['id']}/tools/call", headers=headers, json=body).json()
    assert first['commit'] == NEXT
    stale = client.post(f"/broker/{second['id']}/tools/call", headers=second_headers, json=body).json()
    assert 'head changed' in stale['error']
    assert api.branch == NEXT and len(app.state.store.rows('SELECT * FROM github_followups')) == 2
    assert len([c for c in api.calls if c[0] == 'PATCH']) == 1


@pytest.mark.parametrize('operation', ['update', 'comment'])
def test_two_brokers_cannot_repeat_followup_effect(published, monkeypatch, operation):
    _, _, run, _, github, api = published
    second = GitHub(github.store, github.security, github.settings, github.connectors)
    monkeypatch.setattr(second, 'installation_token', github.installation_token)
    args = update_args() if operation == 'update' else comment_args()

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(method, path, **kwargs):
            if method == ('PATCH' if operation == 'update' else 'POST') and (
                    operation == 'update' or path.endswith('/comments')):
                entered.set()
                await release.wait()
            return await api.request(method, path, **kwargs)

        monkeypatch.setattr(github, 'request', delayed)
        monkeypatch.setattr(second, 'request', delayed)
        first = asyncio.create_task(getattr(github, operation)(run, args))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with pytest.raises(ConnectorError, match='unconfirmed|not confirmed'):
                await asyncio.wait_for(getattr(second, operation)(run, args), 5)
        finally:
            release.set()
        result = await first
        assert await getattr(second, operation)(run, args) == result

    asyncio.run(exercise())
    writes = [entry for entry in api.calls if entry[0] == ('PATCH' if operation == 'update' else 'POST')
              and (operation == 'update' or entry[1].endswith('/comments'))]
    assert len(writes) == 1


def test_uncertain_update_with_absent_readback_never_repeats(published, monkeypatch):
    _, _, run, _, github, api = published
    attempts = []

    async def request(method, path, **kwargs):
        if method == 'PATCH':
            attempts.append(path)
            raise ConnectorError('Lost update response')
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    for _ in range(2):
        with pytest.raises(ConnectorError):
            asyncio.run(github.update(run, update_args()))
    assert len(attempts) == 1


def test_existing_ref_uses_native_protection_without_unrelated_classic_read(published, monkeypatch):
    _, _, run, _, github, api = published

    async def request(method, path, **kwargs):
        if path == '/graphql':
            pytest.fail('Existing refs have provider-native protection metadata.')
        return await api.request(method, path, **kwargs)

    monkeypatch.setattr(github, 'request', request)
    assert asyncio.run(github.update(run, update_args()))['commit'] == NEXT


@pytest.mark.parametrize('policy', ['default', 'protected'])
def test_policy_recheck_preserves_already_claimed_update_uncertainty(published, policy):
    _, _, run, _, github, api = published
    api.lose = 'update'
    with pytest.raises(ConnectorError, match='Lost'):
        asyncio.run(github.update(run, update_args()))
    branch = api.pr['head']['ref']
    if policy == 'default':
        api.default_branch = branch
    else:
        api.protected.add(branch)
    api.calls.clear()
    with pytest.raises(ConnectorError) as pending:
        asyncio.run(github.update(run, update_args()))
    result = pending.value.receipt(write=True)
    assert result['outcome'] == 'unknown' and result['outcome_uncertain'] is True
    assert result['dispatched'] is True and not api.mutations


@pytest.mark.parametrize('default', ['', None])
def test_new_optional_branch_fields_preserve_existing_metadata_hashes(published, default):
    from pydantic import create_model
    from app.github_coding import PRDetails
    _, _, _, _, github, _ = published
    compatible = create_model('CompatibleDetails', __base__=PRDetails,
                              base_branch=(str | None, default), head_branch=(str | None, default))
    args = compatible(repository_id=101, repository=PAYLOAD['repository'], number=100, head_sha=COMMIT,
                      request_key='legacy-details-123', title='A new title')
    original = args.model_dump(exclude={'base_branch', 'head_branch'})
    canonical = {key: value for key, value in original.items() if key != 'repository'}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    assert github.arguments_hash(args, 101) == fingerprint
    assert github.compatible_arguments(args)['body'] is None
    legacy = {key: value for key, value in original.items() if key != 'repository_id'}
    old_hash = hashlib.sha256(json.dumps(legacy).encode()).hexdigest()
    github.store.execute('''INSERT INTO github_followups(id,arguments_hash,connection_version,result)
        VALUES(?,?,?,?)''', ('legacy-details', old_hash, github.connection_version(),
                           json.dumps({'repository': PAYLOAD['repository'], 'repository_id': 101})))
    row = github.store.rows("SELECT * FROM github_followups WHERE id='legacy-details'")[0]
    github.upgrade_receipt_hash('github_followups', row, args, fingerprint)
    assert row['arguments_hash'] == fingerprint


def test_stale_head_and_race_after_commit_do_not_overwrite(published):
    app, client, run, headers, github, api = published
    api.branch = BASE
    with pytest.raises(ConnectorError, match='head changed'):
        asyncio.run(github.update(run, update_args()))
    assert not any(c[0] != 'GET' for c in api.calls)
    api.branch = COMMIT
    def race(method, path):
        if method == 'POST' and path.endswith('/git/commits'):
            api.branch = BASE
    api.on_call = race
    with pytest.raises(ConnectorError, match='changed externally'):
        asyncio.run(github.update(run, update_args()))
    assert not any(c[0] == 'PATCH' for c in api.calls)


def test_revocation_before_branch_update(published):
    app, client, run, headers, github, api = published
    def revoke(method, path):
        if method == 'POST' and path.endswith('/git/commits'):
            app.state.store.update_run(run['id'], status='cancelled')
    api.on_call = revoke
    with pytest.raises(ConnectorError, match='changed'):
        asyncio.run(github.update(run, update_args()))
    assert not any(c[0] == 'PATCH' for c in api.calls)


def test_uncertain_missing_comment_is_not_reposted(published):
    app, client, run, headers, github, api = published
    api.lose = 'comment'
    with pytest.raises(ConnectorError):
        asyncio.run(github.comment(run, comment_args()))
    api.comments.clear()
    with pytest.raises(ConnectorError, match='not confirmed'):
        asyncio.run(github.comment(run, comment_args()))
    assert len([c for c in api.calls if c[0] == 'POST']) == 1


def test_broker_exposes_followups_without_approval_and_reads_feedback(published):
    app, client, run, headers, github, api = published
    url = f"/broker/{run['id']}/tools/call"
    for operation, args in [('update', update_args()), ('comment', comment_args())]:
        name = 'github_' + operation + '_pull_request'
        response = client.post(url, headers=headers, json={'name': name, 'arguments': args.model_dump()})
        assert response.status_code == 200 and 'url' in response.json()
        assert not app.state.store.approvals(run['id'])
    for kind in ['discussion', 'inline', 'reviews']:
        response = client.post(url, headers=headers, json={'name': 'github_pull_request_comments',
            'arguments': {'repository': api.repository, 'number': 100, 'kind': kind}})
        assert response.json()['items'][0]['body'].startswith('@review-bot review')
        assert response.json()['next_page'] is None
    api.comments *= 30
    result = asyncio.run(github.call(run, 'github_pull_request_comments', {'number': 100, 'page': 2}))
    assert result['next_page'] == 3 and api.calls[-1][2]['params']['page'] == 2


def test_large_schema_passes_server_envelope_and_blob_publication(workspace, monkeypatch):
    from sandbox.broker_transport import send_payload
    app, client = workspace
    run_id, headers = connected(app)
    api = GitHubAPI(app.state.connectors.github, monkeypatch)
    # Larger than the reported 2,931,023-byte schema and the former broker cap.
    content = '// generated schema\n' + 'x' * (6 * 1024 * 1024)
    body = {'name': 'github_create_pull_request', 'arguments': {**PAYLOAD, 'files': [{'path': 'schema.d.ts', 'content': content}]}}
    raw = json.dumps(body).encode()
    assert len(raw) > MAX_BODY
    parts = []
    def send(path, value):
        parts.append(path)
        raw = json.dumps(value, ensure_ascii=False).encode()
        packet = seal('run-capability-only', path, raw)
        assert unseal('run-capability-only', path, packet) == raw
        response = client.post(f'/broker/{run_id}'+path, headers={**headers, 'Content-Type': CONTENT_TYPE}, content=packet)
        assert response.status_code == 200, response.text
        return response
    response = send_payload('/tools/call', body, send)
    assert response.status_code == 200 and response.json()['number'] == 100
    assert '/payloads' in parts and parts[-1] == '/tools/call'
    blob = next(c[2]['json'] for c in api.calls if c[1].endswith('/git/blobs'))
    assert blob == {'content': content, 'encoding': 'utf-8'}
    tree = next(c[2]['json']['tree'] for c in api.calls if c[0] == 'POST' and c[1].endswith('/git/trees'))
    assert 'content' not in tree[0] and tree[0]['sha']


def test_complete_utf8_content_beyond_former_byte_and_total_limits():
    assert Change(path='schema.d.ts', content='x' * 2_931_023)
    content = 'é' * (6 * 1024 * 1024)
    files = [{'path': str(i), 'content': content} for i in range(2)]
    parsed = Publish.model_validate({**PAYLOAD, 'body': 'Complete rationale ' * 2000, 'files': files})
    assert [file.content for file in parsed.files] == [content, content]
    with pytest.raises(ValidationError):
        Comment(number=100, body='   ', request_key='empty-comment')
