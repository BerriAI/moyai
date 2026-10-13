"""Supplementary stack tests using the real broker, database and HTTP adapter."""
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.connector_errors import ConnectorError
from app.github import GitHub
from app.github_stacks import API_VERSION, ExpectedPR, GitHubStacks, TOOLS, WRITES
from app.security import digest
from scripts.github_coding_fixture import CodingProvider, REPOSITORY
from test_github import connected
from test_workspace import workspace


STACKS = '/repos/BerriAI/litellm/stacks'
StackBase = GitHub if issubclass(GitHub, GitHubStacks) else type('StackBase', (GitHubStacks, GitHub), {})


class WiredGitHub(StackBase):
    async def call(self, run, name, arguments):
        if name in TOOLS:
            return await self.stack_call(run, name, arguments)
        return await super().call(run, name, arguments)


class StackProvider(CodingProvider):
    def __init__(self):
        super().__init__()
        self.prs = {}
        for number in range(100, 104):
            self.prs[number] = {'number': number, 'state': 'open', 'draft': False, 'merged': False, 'merged_at': None,
                'node_id': f'PR_{number}', 'title': f'Change {number}', 'html_url': f'https://github.com/BerriAI/litellm/pull/{number}',
                'head': {'ref': f'moyai/stack-{number}', 'sha': f'{number:040x}', 'repo': {'id': 101}},
                'base': {'ref': 'main' if number == 100 else f'moyai/stack-{number - 1}',
                         'sha': 'a' * 40 if number == 100 else f'{number - 1:040x}', 'repo': {'id': 101}}}
        self.members = [100, 101]
        self.remaining = []
        self.hook = None
        self.reads = {}
        self.invalid_response = None
        self.stack_status = None
        self.requests = []
        self.default_branch = 'main'
        self.protected_heads = set()
        self.missing_heads = set()
        self.head_rules = {}
        self.policy_status = None
        self.policy_reads = []

    def stack(self):
        return {'id': 900, 'number': 7, 'node_id': 'STACK_7',
                'url': 'https://api.github.com' + STACKS + '/7', 'base': {'ref': 'main'},
                'open': any(self.prs[number]['state'] == 'open' for number in self.members),
                'created_at': '2026-10-13T12:00:00Z', 'pull_requests': [copy.deepcopy(self.prs[number]) for number in self.members]}

    def expected(self, numbers):
        return [ExpectedPR(number=number, head_repository_id=pr['head']['repo']['id'],
            head_ref=pr['head']['ref'], head_sha=pr['head']['sha'], base_repository_id=pr['base']['repo']['id'],
            base_ref=pr['base']['ref'], base_sha=pr['base']['sha'], state=pr['state'], draft=pr['draft'],
            merged_at=pr['merged_at']).model_dump() for number in numbers for pr in [self.prs[number]]]

    def handle(self, request):
        path, method = request.url.path, request.method
        if method == 'GET' and (path == '/repositories/101'
                or path.startswith(('/repositories/101/branches/', '/repositories/101/rules/branches/'))):
            token = request.headers['authorization'].removeprefix('Bearer ')
            permissions = self.tokens[token]
            if path == '/repositories/101':
                data = {**REPOSITORY, 'default_branch': self.default_branch}
            else:
                assert permissions == {'contents': 'read'}
                branch = path.split('/branches/', 1)[1]
                self.policy_reads.append((path, permissions))
                if self.policy_status:
                    return httpx.Response(self.policy_status, json={'message': 'Fixture policy unavailable'})
                if '/rules/branches/' in path:
                    data = self.head_rules.get(branch, [])
                else:
                    if branch in self.missing_heads:
                        return httpx.Response(404, json={'message': 'Fixture branch missing'})
                    data = {'name': branch, 'protected': branch in self.protected_heads,
                            'commit': {'sha': next(pr['head']['sha'] for pr in self.prs.values()
                                                  if pr['head']['ref'] == branch)}}
            response = httpx.Response(200, json=data)
            self.reads[path] = self.reads.get(path, 0) + 1
            if self.hook:
                self.hook(path, self.reads[path])
            return response
        is_pr = path.startswith('/repositories/101/pulls/') and method == 'GET'
        if not is_pr and not path.startswith(STACKS):
            return super().handle(request)
        token = request.headers['authorization'].removeprefix('Bearer ')
        permissions = self.tokens[token]
        assert permissions == {'pull_requests': 'read' if method == 'GET' and permissions['pull_requests'] == 'read' else 'write'}
        if not is_pr:
            assert request.headers['x-github-api-version'] == API_VERSION
        body = json.loads(request.content) if request.content else {}
        self.requests.append((method, path, body))
        if method == 'GET':
            if is_pr:
                data = copy.deepcopy(self.prs[int(path.rsplit('/', 1)[1])])
            elif path == STACKS:
                selected = request.url.params.get('pull_request')
                data = [self.stack()] if self.members and (selected is None or int(selected) in self.members) else []
                for stack in data:
                    stack['pull_requests'] = [{key: member[key] for key in ('number', 'state', 'draft', 'merged_at', 'head')}
                                              for member in stack['pull_requests']]
                    for member in stack['pull_requests']:
                        member['head'].pop('repo')
            elif path == STACKS + '/7':
                if self.members is None:
                    return httpx.Response(404, json={'message': 'Not found'})
                data = self.stack()
            else:
                raise AssertionError(path)
            self.reads[path] = self.reads.get(path, 0) + 1
            if self.hook:
                self.hook(path, self.reads[path])
            return httpx.Response(200, json=data)
        assert method == 'POST' and permissions == {'pull_requests': 'write'}
        self.dispatches.append((method, path, body))
        if self.stack_status:
            return httpx.Response(self.stack_status, json={'message': 'Rejected'})
        if path == STACKS:
            assert set(body) == {'pull_requests'}
            self.members = body['pull_requests']
            status, data = 201, self.stack()
        elif path == STACKS + '/7/add':
            assert set(body) == {'pull_requests'}
            self.members += body['pull_requests']
            status, data = 200, self.stack()
        elif path == STACKS + '/7/unstack':
            assert body == {}
            self.members = list(self.remaining)
            status, data = (200, self.stack()) if self.members else (204, None)
        else:
            raise AssertionError(path)
        if self.lose_response:
            raise httpx.ReadTimeout('Fixture response loss', request=request)
        if self.invalid_response:
            self.invalid_response(data)
        return httpx.Response(status, json=data) if status != 204 else httpx.Response(204)


@pytest.fixture
def stacks(workspace, monkeypatch):
    from app.connectors import TOOLS as CONNECTOR_TOOLS
    app, client = workspace
    _, headers = connected(app)
    store = app.state.store
    run = store.create_run('Stack work', '', 'modal', ['github'], chat_enabled=True, user_id='shared:local:admin')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('run-capability-only'))
    previous = app.state.connectors.github
    github = WiredGitHub(previous.store, previous.security, previous.settings, previous.connectors)
    monkeypatch.setattr(app.state.connectors, 'github', github)
    for name, spec in TOOLS.items():
        monkeypatch.setitem(CONNECTOR_TOOLS, name, spec)
    provider = StackProvider()
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'fixture-jwt')
    original = httpx.AsyncClient
    monkeypatch.setattr('app.github.httpx.AsyncClient',
                        lambda **options: original(transport=httpx.MockTransport(provider.handle), **options))
    for number, pr in provider.prs.items():
        store.execute('''INSERT INTO github_publications
            (id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at,commit_sha)
            VALUES(?,?,?,?,?,?,?,?,?)''', (f'publication-{number}', run['id'], 0, 'fixture', pr['head']['ref'],
            json.dumps({'number': number, 'repository_id': 101}), github.connection_version(), 'fixture', pr['head']['sha']))
        store.execute('''INSERT INTO github_publication_intents
            (id,repository_id,tool,base_branch,base_sha,source_sha,head_repository_id,
             head_branch,connection_version,number,phase) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (f'publication-{number}', 101, 'github_create_pull_request', pr['base']['ref'], pr['base']['sha'],
             pr['base']['sha'], 101, pr['head']['ref'], github.connection_version(), number, 'complete'))
        store.execute('''INSERT INTO github_owned_branches
            (repository_id,branch,connection_version,publication_id,run_id,source_sha) VALUES(?,?,?,?,?,?)''',
            (101, pr['head']['ref'], github.connection_version(), f'publication-{number}', run['id'], pr['base']['sha']))
        store.execute('''INSERT INTO github_publication_effects(publication_id,effect,response) VALUES(?,?,?)''',
            (f'publication-{number}', 'branch', json.dumps({'ref': 'refs/heads/' + pr['head']['ref'],
                'object': {'type': 'commit', 'sha': pr['head']['sha']}})))

    def call(name, **arguments):
        response = client.post(f'/broker/{run["id"]}/tools/call', headers=headers,
            json={'name': name, 'arguments': {'repository_id': 101, **arguments}})
        assert response.status_code in {200, 400}, response.text
        return response.json()

    def existing():
        result = call('github_stack', stack_number=7)
        assert 'error' not in result, result
        return {'stack_number': 7, 'revision': result['revision'], 'members': result['members']}

    return SimpleNamespace(app=app, client=client, store=store, github=github, provider=provider, call=call,
                           existing=existing, run_id=run['id'])


def arguments(stacks, name):
    if name == 'github_create_stack':
        stacks.provider.members = None
        values = {'pull_requests': stacks.provider.expected([100, 101])}
    else:
        values = stacks.existing()
        if name == 'github_add_to_stack':
            values['pull_requests'] = stacks.provider.expected([102])
    return {'request_key': 'stack-operation-1', **values}


def grant(stacks, number):
    pr = stacks.provider.prs[number]
    stacks.store.execute('DELETE FROM github_publications WHERE id=?', (f'publication-{number}',))
    stacks.store.execute('DELETE FROM github_owned_branches WHERE repository_id=? AND branch=?',
                         (pr['head']['repo']['id'], pr['head']['ref']))
    stacks.store.execute('''INSERT INTO github_write_access VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (f'grant-{number}', stacks.run_id, 'shared:local:admin', stacks.github.connection_version(), 101, number,
         pr['head']['repo']['id'], pr['head']['ref'], pr['base']['ref'], 'Fixture', 'approved', 'fixture'))


def test_read_schema_pagination_and_readonly_discovery(stacks):
    stacks.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    result = stacks.call('github_stacks', per_page=1)
    assert result['next_page'] == 2 and len(result['items']) == 1
    assert 'base' not in result['items'][0]['pull_requests'][0]
    result = stacks.call('github_stack', stack_number=7)
    assert result['members'] == stacks.provider.expected([100, 101])
    assert len(result['revision']) == 64
    assert all(permissions.get('pull_requests') != 'write' for permissions in stacks.provider.minted)
    assert 'error' in stacks.call('github_create_stack', request_key='stack-create-1',
                                  pull_requests=stacks.provider.expected([100, 101]))
    assert stacks.provider.dispatches == []


def test_readers_do_not_require_head_branch_ownership_or_contents_scope(stacks):
    stacks.store.execute('DELETE FROM github_owned_branches')
    stacks.provider.protected_heads.update(pr['head']['ref'] for pr in stacks.provider.prs.values())
    assert 'error' not in stacks.call('github_stacks')
    assert 'error' not in stacks.call('github_stack', stack_number=7)
    assert stacks.provider.policy_reads == []
    assert {'contents': 'read'} not in stacks.provider.minted


@pytest.mark.parametrize('name', list(WRITES))
def test_app_created_pr_over_unowned_head_does_not_authorize_stack(stacks, name):
    stacks.store.execute("DELETE FROM github_owned_branches WHERE branch='moyai/stack-101'")
    stacks.store.execute("UPDATE github_publication_intents SET tool='github_open_pull_request' WHERE id='publication-101'")
    args = arguments(stacks, name)
    assert 'error' in stacks.call(name, **args)
    assert stacks.provider.dispatches == []


@pytest.mark.parametrize('name', list(WRITES))
def test_exact_external_grants_allow_writable_heads_using_actual_tool(stacks, monkeypatch, name):
    numbers = [100, 101, 102] if name == 'github_add_to_stack' else [100, 101]
    for number in numbers:
        grant(stacks, number)
    checked = []
    original = stacks.github.writable_branch

    async def inspect_branch(run, token, target, branch, version, tool, *, existing=False, **options):
        checked.append((target, branch, tool, existing))
        return await original(run, token, target, branch, version, tool, existing=existing, **options)

    monkeypatch.setattr(stacks.github, 'writable_branch', inspect_branch)
    args = arguments(stacks, name)
    result = stacks.call(name, **args)
    assert result.get('confirmed'), result
    assert checked == [(101, f'moyai/stack-{number}', name, True) for _ in range(2) for number in numbers]
    assert {'contents': 'read'} in stacks.provider.minted
    assert {'contents': 'write'} not in stacks.provider.minted
    assert all(permissions == {'contents': 'read'} for _, permissions in stacks.provider.policy_reads)


@pytest.mark.parametrize('name', list(WRITES))
@pytest.mark.parametrize('restriction', ['default', 'protected', 'rules', 'missing', 'unavailable',
                                       'contents_permission', 'unselected_head'])
def test_every_mutation_checks_head_policy(stacks, name, restriction):
    number = 102 if name == 'github_add_to_stack' else 101
    branch = stacks.provider.prs[number]['head']['ref']
    if restriction == 'default':
        stacks.provider.default_branch = branch
    elif restriction == 'protected':
        stacks.provider.protected_heads.add(branch)
    elif restriction == 'rules':
        stacks.provider.head_rules[branch] = [{'type': 'required_status_checks'}]
    elif restriction == 'missing':
        stacks.provider.missing_heads.add(branch)
    elif restriction == 'unavailable':
        stacks.provider.policy_status = 403
    elif restriction == 'unselected_head':
        stacks.provider.prs[number]['head']['repo']['id'] = 202
        grant(stacks, number)
    args = arguments(stacks, name)
    if restriction == 'contents_permission':
        stacks.provider.permissions.pop('contents')
    assert 'error' in stacks.call(name, **args)
    assert stacks.provider.dispatches == []
    assert all(row['sent'] == 0 for row in stacks.store.rows('SELECT sent FROM github_followups'))


@pytest.mark.parametrize('name', list(WRITES))
def test_missing_contents_read_token_never_falls_back_to_broader_scope(stacks, monkeypatch, name):
    args = arguments(stacks, name)
    original = stacks.github.installation_token
    attempts = []

    async def token(*positional, **options):
        attempts.append(options.get('coding'))
        if options.get('coding') == 'contents_read':
            raise ConnectorError('Fixture Contents read unavailable.', dispatched=False)
        return await original(*positional, **options)

    monkeypatch.setattr(stacks.github, 'installation_token', token)
    with pytest.raises(ConnectorError) as failure:
        asyncio.run(stacks.github.stack_call(stacks.store.run(stacks.run_id), name, {'repository_id': 101, **args}))
    assert str(failure.value) == 'Fixture Contents read unavailable.'
    assert attempts[-1] == 'contents_read', attempts
    assert stacks.provider.policy_reads == [] and stacks.provider.dispatches == []


@pytest.mark.parametrize('change', ['grant', 'ownership', 'protection', 'head', 'membership_grant'])
def test_policy_reads_cannot_keep_revoked_authority_or_stale_heads(stacks, change):
    if change in {'grant', 'membership_grant'}:
        grant(stacks, 100)
    args = arguments(stacks, 'github_add_to_stack')
    stacks.provider.reads.clear()

    def change_during_policy(path, count):
        if change == 'protection':
            if path == STACKS + '/7' and count == 1:
                stacks.provider.protected_heads.add('moyai/stack-100')
            return
        trigger = STACKS + '/7' if change == 'membership_grant' else '/repositories/101/rules/branches/moyai/stack-102'
        if path != trigger or count != 2:
            return
        if change in {'grant', 'membership_grant'}:
            stacks.store.execute("UPDATE github_write_access SET status='revoked' WHERE id='grant-100'")
        elif change == 'ownership':
            stacks.store.execute("DELETE FROM github_owned_branches WHERE branch='moyai/stack-100'")
        else:
            stacks.provider.prs[102]['head']['sha'] = 'f' * 40

    stacks.provider.hook = change_during_policy
    assert 'error' in stacks.call('github_add_to_stack', **args)
    assert stacks.provider.dispatches == []
    assert all(row['sent'] == 0 for row in stacks.store.rows('SELECT sent FROM github_followups'))


@pytest.mark.parametrize('name', list(WRITES))
def test_write_receipts_replay_without_second_dispatch(stacks, name):
    args = arguments(stacks, name)
    first = stacks.call(name, **args)
    assert first.get('confirmed') is True, first
    assert first['status_code'] == (201 if name == 'github_create_stack' else 204 if name == 'github_unstack' else 200)
    assert stacks.call(name, **args) == first
    assert len(stacks.provider.dispatches) == 1
    assert first['scope']['members'] == (
        args['pull_requests'] if name == 'github_create_stack' else args['members'] + args.get('pull_requests', []))
    assert set(first['scope']['authorities']) == {str(member['number']) for member in first['scope']['members']}
    assert all(stacks.provider.reads[f'/repositories/101/pulls/{number}'] == 4
               for number in (100, 101, 102) if str(number) in first['scope']['authorities'])
    assert all('server-token' not in value for value in [json.dumps(first)])


def test_partial_unstack_is_not_dissolution(stacks):
    args = arguments(stacks, 'github_unstack')
    stacks.provider.remaining = [101]
    result = stacks.call('github_unstack', **args)
    assert result['confirmed'] and result['outcome'] == 'partial_unstack'
    assert result['status_code'] == 200 and result['remaining_prs'] == [101] and result['removed_prs'] == [100]
    assert result['stack_id'] == 900 and result['data']['number'] == 7


@pytest.mark.parametrize('name', list(WRITES))
def test_unknown_dispatch_survives_broker_restart(stacks, monkeypatch, name):
    args = arguments(stacks, name)
    stacks.provider.lose_response = True
    result = stacks.call(name, **args)
    assert result['code'] == 'outcome_unknown' and result['outcome_uncertain']
    restored = WiredGitHub(stacks.github.store, stacks.github.security, stacks.github.settings, stacks.github.connectors)
    monkeypatch.setattr(restored, 'app_jwt', lambda config=None: 'fixture-jwt')
    monkeypatch.setattr(stacks.app.state.connectors, 'github', restored)
    stacks.provider.lose_response = False
    result = stacks.call(name, **args)
    assert result['code'] == 'outcome_unknown' and len(stacks.provider.dispatches) == 1
    row = stacks.store.rows('SELECT * FROM github_followups')[0]
    assert row['sent'] == 1
    assert json.loads(row['result'])['phase'] == 'prepared'
    assert len(json.loads(row['result'])['scope']['members']) >= 2


@pytest.mark.parametrize('restriction', ['missing_grant', 'stale_head', 'stale_base', 'foreign_base', 'foreign_head',
                                       'wrong_revision', 'missing_member', 'chain', 'unselected', 'new_existing_member'])
def test_invalid_scope_never_dispatches(stacks, restriction):
    name = 'github_add_to_stack'
    args = arguments(stacks, name)
    if restriction == 'missing_grant':
        stacks.store.execute("DELETE FROM github_publications WHERE id='publication-101'")
    elif restriction in {'stale_head', 'stale_base', 'foreign_base', 'foreign_head'}:
        field, value = {'stale_head': ('head_sha', 'f' * 40), 'stale_base': ('base_sha', 'f' * 40),
                        'foreign_base': ('base_repository_id', 202), 'foreign_head': ('head_repository_id', 202)}[restriction]
        args['pull_requests'][0][field] = value
    elif restriction == 'wrong_revision':
        args['revision'] = 'f' * 64
    elif restriction == 'missing_member':
        args['members'].pop()
    elif restriction == 'chain':
        stacks.provider.prs[102]['base']['ref'] = 'main'
        args['pull_requests'] = stacks.provider.expected([102])
    elif restriction == 'new_existing_member':
        args['pull_requests'] = stacks.provider.expected([101])
    else:
        args['repository_id'] = 202
    assert 'error' in stacks.call(name, **args)
    assert stacks.provider.dispatches == []


@pytest.mark.parametrize('change', ['membership', 'late_membership', 'head', 'base', 'repository',
                                   'revoke_first', 'requester', 'readonly'])
def test_second_pass_and_all_authorities_checked(stacks, change):
    grant(stacks, 100)
    grant(stacks, 101)
    args = arguments(stacks, 'github_add_to_stack')
    stacks.provider.reads.clear()

    def change_after_read(path, count):
        if path != '/repositories/101/pulls/102' or count != (4 if change in {'revoke_first', 'late_membership'} else 1):
            return
        if change in {'membership', 'late_membership'}:
            stacks.provider.members.append(103)
        elif change in {'head', 'base'}:
            stacks.provider.prs[100][change]['sha'] = 'f' * 40
        elif change == 'repository':
            stacks.provider.prs[100]['head']['repo']['id'] = 202
        elif change == 'revoke_first':
            stacks.store.execute("UPDATE github_write_access SET status='revoked' WHERE id='grant-100'")
        elif change == 'requester':
            stacks.store.execute("UPDATE runs SET active_user_id='another-user' WHERE id=?", (stacks.run_id,))
        else:
            stacks.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")

    stacks.provider.hook = change_after_read
    assert 'error' in stacks.call('github_add_to_stack', **args)
    assert stacks.provider.dispatches == []


def test_one_grant_cannot_authorize_another_member(stacks):
    grant(stacks, 100)
    stacks.store.execute("DELETE FROM github_publications WHERE id='publication-101'")
    args = arguments(stacks, 'github_add_to_stack')
    assert 'error' in stacks.call('github_add_to_stack', **args)
    assert stacks.provider.dispatches == []


def test_receipt_replay_requires_every_grant(stacks):
    grant(stacks, 100)
    grant(stacks, 101)
    args = arguments(stacks, 'github_create_stack')
    assert stacks.call('github_create_stack', **args)['confirmed']
    stacks.store.execute("UPDATE github_write_access SET status='revoked' WHERE id='grant-100'")
    assert 'error' in stacks.call('github_create_stack', **args)
    assert len(stacks.provider.dispatches) == 1


def test_closed_unmerged_member_can_be_unstacked(stacks):
    stacks.provider.prs[100]['state'] = 'closed'
    grant(stacks, 100)
    args = arguments(stacks, 'github_unstack')
    assert stacks.call('github_unstack', **args)['outcome'] == 'dissolved'


def test_merged_history_is_fenced_but_not_mutation_authority(stacks):
    stacks.provider.prs[100].update(state='closed', merged=True, merged_at='2026-10-12T10:00:00Z')
    stacks.store.execute("DELETE FROM github_publications WHERE id='publication-100'")
    stacks.provider.remaining = [100]
    args = arguments(stacks, 'github_unstack')
    result = stacks.call('github_unstack', **args)
    assert result['outcome'] == 'partial_unstack'
    assert set(result['scope']['authorities']) == {'101'}
    assert all('stack-100' not in path for path, _ in stacks.provider.policy_reads)


@pytest.mark.parametrize('corruption', ['wrong_number', 'wrong_repo', 'wrong_members', 'wrong_status', 'missing_base_sha'])
def test_invalid_provider_confirmation_remains_unknown(stacks, corruption):
    args = arguments(stacks, 'github_add_to_stack')
    if corruption == 'wrong_status':
        stacks.provider.stack_status = 409
    else:
        def corrupt(data):
            if corruption == 'wrong_number':
                data['number'] = 8
            elif corruption == 'wrong_repo':
                data['pull_requests'][0]['base']['repo']['id'] = 202
            elif corruption == 'wrong_members':
                data['pull_requests'].pop()
            else:
                data['pull_requests'][0]['base'].pop('sha')
        stacks.provider.invalid_response = corrupt
    assert stacks.call('github_add_to_stack', **args)['code'] == 'outcome_unknown'
    assert stacks.call('github_add_to_stack', **args)['code'] == 'outcome_unknown'
    assert len(stacks.provider.dispatches) == 1


def test_confirmation_storage_failure_never_repeats(stacks, monkeypatch):
    args = arguments(stacks, 'github_create_stack')
    original = stacks.store.execute

    def execute(sql, parameters=(), **options):
        if sql.startswith('UPDATE github_followups SET result=') and 'sent=1' in sql:
            raise RuntimeError('Fixture persistence failure')
        return original(sql, parameters, **options)

    monkeypatch.setattr(stacks.store, 'execute', execute)
    assert stacks.call('github_create_stack', **args)['code'] == 'outcome_unknown'
    monkeypatch.setattr(stacks.store, 'execute', original)
    assert stacks.call('github_create_stack', **args)['code'] == 'outcome_unknown'
    assert len(stacks.provider.dispatches) == 1


@pytest.mark.parametrize('field,value', [('event', 'APPROVE'), ('merge', True), ('queue', True),
                                       ('url', 'https://example.com'), ('query', 'mutation{}'),
                                       ('pull_requests', [100])])
def test_unstack_schema_rejects_selective_and_forbidden_fields(stacks, field, value):
    args = arguments(stacks, 'github_unstack')
    args[field] = value
    with pytest.raises(ValidationError):
        TOOLS['github_unstack'][2].model_validate({'repository_id': 101, **args})
    assert stacks.provider.dispatches == []


def test_bounds_and_closed_member_schemas(stacks):
    member = stacks.provider.expected([100])[0]
    for values in ([], [member], [member, member], [member] * 101):
        with pytest.raises(ValidationError):
            TOOLS['github_create_stack'][2].model_validate(
                {'repository_id': 101, 'request_key': 'bounded-test', 'pull_requests': values})
    with pytest.raises(ValidationError):
        ExpectedPR.model_validate({**member, 'head_sha': 'HEAD~1'})
    with pytest.raises(ValidationError):
        ExpectedPR.model_validate({**member, 'base_ref': 'main..unsafe'})
    with pytest.raises(ValidationError):
        ExpectedPR.model_validate({**member, 'number': True})
    with pytest.raises(ValidationError):
        ExpectedPR.model_validate({**member, 'approval': True})
    with pytest.raises(ValidationError):
        ExpectedPR.model_validate({**member, 'merged_at': '2026-10-13T12:00:00Z'})


def test_create_checks_existing_membership(stacks):
    result = stacks.call('github_create_stack', request_key='stack-conflict-1',
                         pull_requests=stacks.provider.expected([100, 101]))
    assert 'error' in result and stacks.provider.dispatches == []


def test_changed_request_key_payload_is_not_replayed(stacks):
    args = arguments(stacks, 'github_create_stack')
    assert stacks.call('github_create_stack', **args)['confirmed']
    args['pull_requests'].append(stacks.provider.expected([102])[0])
    assert 'error' in stacks.call('github_create_stack', **args)
    assert len(stacks.provider.dispatches) == 1


def test_unstack_cannot_claim_merged_history_was_removed(stacks):
    stacks.provider.prs[100].update(state='closed', merged=True, merged_at='2026-10-12T10:00:00Z')
    args = arguments(stacks, 'github_unstack')
    assert stacks.call('github_unstack', **args)['code'] == 'outcome_unknown'
    assert len(stacks.provider.dispatches) == 1


def test_cross_broker_claim_uses_durable_cas(stacks, monkeypatch):
    args = {'repository_id': 101, **arguments(stacks, 'github_create_stack')}
    other = WiredGitHub(stacks.github.store, stacks.github.security, stacks.github.settings, stacks.github.connectors)
    monkeypatch.setattr(other, 'app_jwt', lambda config=None: 'fixture-jwt')
    original = stacks.github.request

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def waiting_request(method, path, **options):
            if method == 'POST' and path == STACKS:
                entered.set()
                await release.wait()
            return await original(method, path, **options)

        monkeypatch.setattr(stacks.github, 'request', waiting_request)
        run = stacks.store.run(stacks.run_id)
        first = asyncio.create_task(stacks.github.stack_call(run, 'github_create_stack', args))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            with pytest.raises(ConnectorError) as failure:
                await other.stack_call(run, 'github_create_stack', args)
            assert failure.value.code == 'outcome_unknown'
        finally:
            release.set()
        assert (await first)['confirmed']

    asyncio.run(exercise())
    assert len(stacks.provider.dispatches) == 1
