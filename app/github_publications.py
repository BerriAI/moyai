"""Durable publication intent, claimed effects and trusted branch provenance."""
from collections.abc import Mapping
import hashlib
import json
import re
from typing import TYPE_CHECKING, Literal, TypedDict
from urllib.parse import quote

from agent.github_refs import validate_branch_name
from .connector_errors import ConnectorError
from .db import now
from .github_request_policy import CLASSIC_PROTECTION_QUERY, REBASE_REF_MUTATION

if TYPE_CHECKING:
    from .github import Rebase


PatternToken = tuple[Literal['literal', 'star', 'any', 'set', 'not_set'], frozenset[str]]


class ClassicProtectionRule(TypedDict):
    id: str
    pattern: str
    blocksCreations: bool


def classic_pattern_tokens(component: str) -> list[PatternToken]:
    """Accept a bounded Ruby fnmatch subset; unknown syntax is not a nonmatch."""
    tokens: list[PatternToken] = []
    position = 0
    while position < len(component):
        character = component[position]
        position += 1
        if character == '*':
            if not tokens or tokens[-1][0] != 'star':
                tokens.append(('star', frozenset()))
        elif character == '?':
            tokens.append(('any', frozenset()))
        elif character == '[':
            closing = component.find(']', position)
            if closing < 0:
                raise ValueError('Unsupported classic protection pattern.')
            members = component[position:closing]
            negated = members.startswith(('!', '^'))
            if negated:
                members = members[1:]
            if not members or not re.fullmatch(r'[A-Za-z0-9_.-]+', members):
                raise ValueError('Unsupported classic protection character class.')
            allowed: set[str] = set()
            member = 0
            while member < len(members):
                if member + 2 < len(members) and members[member + 1] == '-':
                    first, last = members[member], members[member + 2]
                    if (first == '-' or last == '-' or ord(first) > ord(last)
                            or not (first.isdigit() and last.isdigit()
                                    or first.islower() and last.islower()
                                    or first.isupper() and last.isupper())):
                        raise ValueError('Unsupported classic protection range.')
                    allowed.update(chr(codepoint) for codepoint in range(ord(first), ord(last) + 1))
                    member += 3
                else:
                    if members[member] == '-' and member not in {0, len(members) - 1}:
                        raise ValueError('Unsupported classic protection range.')
                    allowed.add(members[member])
                    member += 1
            tokens.append(('not_set' if negated else 'set', frozenset(allowed)))
            position = closing + 1
        else:
            tokens.append(('literal', frozenset({character})))
    return tokens


def classic_component_matches(tokens: list[PatternToken], component: str) -> bool:
    """Evaluate one pathname component without backtracking or slash wildcards."""
    positions = [True] + [False] * len(component)
    for kind, members in tokens:
        following = [False] * (len(component) + 1)
        if kind == 'star':
            following[0] = positions[0]
        for index, character in enumerate(component, start=1):
            if kind == 'star':
                following[index] = positions[index] or following[index - 1]
            elif kind == 'any':
                following[index] = positions[index - 1]
            else:
                matches = character not in members if kind == 'not_set' else character in members
                following[index] = positions[index - 1] and matches
        positions = following
    return positions[-1]


def classic_pattern_matches(pattern: str, branch: str) -> bool:
    """Ruby File.fnmatch(..., FNM_PATHNAME) for validated Git branch names."""
    validate_branch_name(branch)
    if (not isinstance(pattern, str) or not 0 < len(pattern) <= 1024 or '\\' in pattern
            or any(ord(character) < 32 or ord(character) == 127 for character in pattern)):
        raise ValueError('Unsupported classic protection pattern.')
    components = pattern.split('/')
    if any(not component for component in components):
        raise ValueError('Unsupported classic protection pathname.')
    parsed = [classic_pattern_tokens(component) for component in components]
    names = branch.split('/')
    positions: set[int] = {0}
    for index, tokens in enumerate(parsed):
        if components[index] == '**' and index < len(components) - 1:
            positions = set(range(min(positions), len(names) + 1)) if positions else set()
        else:
            positions = {position + 1 for position in positions
                         if position < len(names) and classic_component_matches(tokens, names[position])}
    return len(names) in positions


def classic_creation_restricted(rules: list[ClassicProtectionRule], branch: str) -> bool:
    matches = [rule for rule in rules if classic_pattern_matches(rule['pattern'], branch)]
    exact = [rule for rule in matches if rule['pattern'] == branch
             and not any(character in branch for character in '*?]')]
    restrictions = {rule['blocksCreations'] for rule in exact or matches}
    if len(restrictions) > 1:
        raise ValueError('Overlapping classic creation rules have ambiguous priority.')
    return True in restrictions


def confirmed_ref_creation(response: str, branch: str, commit: str) -> bool:
    """Ref state alone cannot prove who won an ambiguous creation race."""
    try:
        acknowledged = json.loads(response)
    except (TypeError, ValueError):
        return False
    pointer = acknowledged.get('object') if isinstance(acknowledged, dict) else None
    return (isinstance(pointer, dict) and acknowledged.get('ref') == 'refs/heads/' + branch
            and pointer.get('type') == 'commit' and pointer.get('sha') == commit)


class GitHubPublications:
    def publication_intent(self, identity):
        rows = self.store.rows('SELECT * FROM github_publication_intents WHERE id=?', (identity,))
        return rows[0] if rows else None

    def publication_check(self, run, version, tool, target, actor=None):
        self.ensure_publish_allowed(run, version, tool)
        self.target(target)
        if actor is not None and actor != run.get('active_user_id', ''):
            raise ConnectorError('The publication requester changed.')

    def effect_claimed(self, identity, effect):
        rows = self.store.rows('SELECT response FROM github_publication_effects WHERE publication_id=? AND effect=?',
                               (identity, effect))
        if rows and rows[0]['response'] and json.loads(rows[0]['response']) == {'rejected': True}:
            raise ConnectorError('GitHub rejected the claimed write. Readback cannot establish publication ownership.')
        return bool(rows)

    def publication_policy_error(self, operation: str, message: str) -> ConnectorError:
        """A local refusal cannot erase an earlier ambiguous ref dispatch."""
        claimed = bool(operation and self.effect_claimed(operation, 'branch'))
        return ConnectorError(message, dispatched=claimed)

    async def publication_effect(self, identity, effect, method, path, **kwargs):
        if not identity:
            raise ConnectorError('A durable operation is required before GitHub writes.')
        claimed = self.store.execute('''INSERT INTO github_publication_effects(publication_id,effect)
            VALUES(?,?) ON CONFLICT DO NOTHING''', (identity, effect))
        if not claimed:
            row = self.store.rows('SELECT response FROM github_publication_effects WHERE publication_id=? AND effect=?',
                                  (identity, effect))[0]
            if row['response']:
                if json.loads(row['response']) == {'rejected': True}:
                    raise ConnectorError('GitHub rejected the claimed write. It will not be repeated.')
                return json.loads(row['response'])
            raise ConnectorError('This write was already claimed. Its outcome is unconfirmed; only read-only recovery is permitted.')
        try:
            response = await self.request(method, path, **kwargs)
        except ConnectorError as error:
            if 400 <= (getattr(error, 'status_code', 0) or 0) < 500 and error.status_code != 408:
                self.store.execute('UPDATE github_publication_effects SET response=? WHERE publication_id=? AND effect=?',
                                   (json.dumps({'rejected': True}), identity, effect))
            raise
        if effect == 'commit' or effect == 'tree' or effect.startswith('blob:'):
            if not isinstance(response, dict) or not re.fullmatch(r'[0-9a-f]{40}', str(response.get('sha', ''))):
                raise ConnectorError('GitHub did not confirm the prepared object.')
        self.store.execute('UPDATE github_publication_effects SET response=? WHERE publication_id=? AND effect=?',
                           (json.dumps(response), identity, effect))
        return response

    async def publication_ref(self, token, target, branch, *, missing=False):
        if not self.valid_branch(branch):
            raise ConnectorError('Invalid publication branch.')
        ref = await self.request('GET', f'/repositories/{target}/git/ref/heads/{quote(branch, safe="/")}',
                                 token=token, missing=missing)
        if ref is None and missing:
            return ''
        sha = ((ref or {}).get('object') or {}).get('sha')
        if (not isinstance(ref, dict) or not re.fullmatch(r'[0-9a-f]{40}', str(sha))
                or ref.get('ref', 'refs/heads/' + branch) != 'refs/heads/' + branch
                or (ref.get('object') or {}).get('type', 'commit') != 'commit'):
            raise ConnectorError('GitHub did not confirm the exact branch and commit.')
        return sha

    async def classic_branch_rules(self, run: Mapping[str, object], token: str, target: int,
                                   version: str, tool: str, *, operation: str = '') -> list[ClassicProtectionRule]:
        owner, name = self.repository_name(target).split('/')
        cursor: str | None = None
        cursors: set[str] = set()
        identifiers: set[str] = set()
        rules: list[ClassicProtectionRule] = []
        total: int | None = None
        for _ in range(10):
            self.publication_check(run, version, tool, target)
            try:
                response = await self.request('POST', '/graphql', token=token,
                    json={'query': CLASSIC_PROTECTION_QUERY, 'variables': {'owner': owner, 'name': name, 'cursor': cursor}})
            except ConnectorError as error:
                dispatched = True if operation and self.effect_claimed(operation, 'branch') else error.dispatched
                raise ConnectorError(f'Classic branch protection read was not confirmed; creation is not authorized. {error}',
                                     code='classic_policy_unavailable', dispatched=dispatched, status_code=error.status_code) from None
            self.publication_check(run, version, tool, target)
            data = response.get('data') if isinstance(response, dict) else None
            repository = data.get('repository') if isinstance(data, dict) else None
            connection = repository.get('branchProtectionRules') if isinstance(repository, dict) else None
            if (not isinstance(response, dict) or response.get('errors')
                    or not isinstance(repository, dict) or type(repository.get('databaseId')) is not int
                    or repository['databaseId'] != target or not isinstance(connection, dict)):
                raise self.publication_policy_error(operation,
                    'Classic branch protection could not be verified. Missing permission or partial results never authorize creation.')
            count, nodes, page = connection.get('totalCount'), connection.get('nodes'), connection.get('pageInfo')
            if (type(count) is not int or not 0 <= count <= 1000 or total is not None and count != total
                    or not isinstance(nodes, list) or len(nodes) > 100 or not isinstance(page, dict)
                    or type(page.get('hasNextPage')) is not bool or 'endCursor' not in page):
                raise self.publication_policy_error(operation, 'Classic branch protection returned incomplete or changing policy.')
            total = count
            for node in nodes:
                if (not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']
                        or node['id'] in identifiers or not isinstance(node.get('pattern'), str)
                        or type(node.get('blocksCreations')) is not bool):
                    raise self.publication_policy_error(operation, 'Classic branch protection returned invalid or repeated rules.')
                identifiers.add(node['id'])
                rules.append({'id': node['id'], 'pattern': node['pattern'], 'blocksCreations': node['blocksCreations']})
            if len(rules) > total:
                raise self.publication_policy_error(operation, 'Classic branch protection returned inconsistent policy counts.')
            if not page['hasNextPage']:
                if len(rules) != total:
                    raise self.publication_policy_error(operation, 'Classic branch protection returned incomplete policy.')
                return rules
            cursor = page['endCursor']
            if (not nodes or not isinstance(cursor, str) or not 0 < len(cursor) <= 1024
                    or cursor in cursors or len(rules) >= total):
                raise self.publication_policy_error(operation, 'Classic branch protection pagination is incomplete.')
            cursors.add(cursor)
        raise self.publication_policy_error(operation, 'Classic branch protection exceeds the bounded policy read.')

    async def writable_branch(self, run: Mapping[str, object], token: str, target: int, branch: str,
                              version: str, tool: str, *, existing: bool = False, operation: str = '') -> None:
        self.publication_check(run, version, tool, target)
        repo = await self.request('GET', f'/repositories/{target}', token=token)
        if repo.get('id') != target or not self.valid_branch(repo.get('default_branch')):
            raise self.publication_policy_error(operation, 'GitHub did not confirm the repository identity.')
        if branch == repo['default_branch']:
            raise self.publication_policy_error(operation, 'Default branch writes are prohibited.')
        metadata = await self.request('GET', f'/repositories/{target}/branches/{quote(branch, safe="")}',
                                      token=token, missing=True)
        if existing and metadata is None:
            raise self.publication_policy_error(operation, 'The existing branch protection could not be verified.')
        if metadata is not None and (not isinstance(metadata, dict) or metadata.get('name') != branch
                                     or metadata.get('protected') is not False):
            raise self.publication_policy_error(operation, 'Protected branch writes are prohibited or protection could not be verified.')
        if metadata is not None and not existing:
            raise self.publication_policy_error(operation, 'The branch already exists. Creation never overwrites an existing ref.')
        if metadata is None:
            classic = await self.classic_branch_rules(run, token, target, version, tool, operation=operation)
            try:
                restricted = classic_creation_restricted(classic, branch)
            except ValueError as error:
                raise self.publication_policy_error(operation, f'Classic branch protection could not be evaluated safely: {error}') from None
            if restricted:
                raise self.publication_policy_error(operation, 'Classic branch protection restricts creation of this branch.')
        rules = await self.request('GET', f'/repositories/{target}/rules/branches/{quote(branch, safe="")}', token=token)
        if not isinstance(rules, list) or rules:
            raise self.publication_policy_error(operation, 'Protected branch writes are prohibited or branch rules could not be verified.')
        self.publication_check(run, version, tool, target)

    def branch_owned(self, target, branch, version):
        rows = self.store.rows('''SELECT publication.commit_sha,effect.response
            FROM github_owned_branches owned
            JOIN github_publications publication ON publication.id=owned.publication_id
            JOIN github_publication_intents intent ON intent.id=publication.id
            JOIN github_publication_effects effect ON effect.publication_id=publication.id AND effect.effect='branch'
            WHERE owned.repository_id=? AND owned.branch=? AND owned.connection_version=?
                AND publication.branch=owned.branch AND publication.connection_version=owned.connection_version
                AND intent.repository_id=owned.repository_id AND intent.expected_head_sha='' ''', (target, branch, version))
        return bool(rows and confirmed_ref_creation(rows[0]['response'], branch, rows[0]['commit_sha']))

    def freeze_followup(self, run, row, args, target, head_target, pr, version, tool):
        identity = 'followup:' + row['id']
        saved = self.publication_intent(identity)
        if not saved and (row['commit_sha'] or row['sent']):
            raise ConnectorError('Legacy follow-up unconfirmed: frozen identity is missing. No write was repeated.')
        values = {'repository_id': target, 'tool': tool, 'base_branch': pr['base']['ref'],
                  'base_sha': pr['base'].get('sha', ''), 'source_sha': getattr(args, 'base_sha', pr['head']['sha']),
                  'expected_head_sha': getattr(args, 'expected_head_sha', getattr(args, 'base_sha', '')),
                  'actor_id': run.get('active_user_id', ''),
                  'head_repository_id': head_target, 'head_branch': pr['head']['ref'], 'connection_version': version,
                  'number': args.number}
        self.store.execute('''INSERT INTO github_publication_intents
            (id,repository_id,tool,base_branch,base_sha,source_sha,expected_head_sha,actor_id,
             head_repository_id,head_branch,connection_version,number,phase)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'preparing') ON CONFLICT DO NOTHING''',
            (identity, *values.values()))
        saved = self.publication_intent(identity)
        frozen_fields = ('repository_id', 'tool', 'base_branch', 'expected_head_sha', 'actor_id',
                         'head_repository_id', 'head_branch', 'connection_version', 'number')
        if tool == 'github_rebase_pull_request':
            frozen_fields += ('base_sha', 'source_sha')
        for field in frozen_fields:
            if saved[field] != values[field]:
                raise ConnectorError('The follow-up publication identity changed. No write was repeated.')
        return identity

    async def rebase_pull_request(self, run: Mapping[str, object], args: 'Rebase') -> dict[str, object]:
        from .github import workflow_changes
        tool = 'github_rebase_pull_request'
        version = run.get('github_connection_version') or self.connection_version()
        self.ensure_publish_allowed(run, version, tool)
        target = await self.selected_target(run, args.repository, args.repository_id)
        self.publication_check(run, version, tool, target)
        token = await self.installation_token(repository=target, write=True)
        authority = await self.rebase_authority(run, token, target, args.number, version)
        if authority.get('grant'):
            run = {**run, '_pr_write_scope': (target, args.number, authority['id'])}
        row = self.followup(run, tool, args, version, target)
        if row['result'] and not authority.get('grant'):
            return self.receipt(row['result'], target)
        pr = await self.authorized_pr(run, token, target, args.number, authority, version, tool)
        if row['result']:
            return self.receipt(row['result'], target)
        operation = 'followup:' + row['id']
        claimed = self.effect_claimed(operation, 'branch')
        head_target, branch, base_branch = self.pr_identity(pr, target, args.number)
        if head_target != authority.get('head_repository_id', target) or branch != authority['branch']:
            raise ConnectorError('The rebase PR head identity changed.', dispatched=False)
        if pr['base'].get('sha') != args.base_sha:
            raise ConnectorError('The prepared checkout does not match the current PR base commit.', dispatched=claimed)
        if await self.publication_ref(token, target, base_branch) != args.base_sha:
            raise ConnectorError('The PR target base changed. Prepare the rebase from its current base.', dispatched=claimed)
        operation = self.freeze_followup(run, row, args, target, head_target, pr, version, tool)
        intent = self.publication_intent(operation)
        self.publication_check(run, version, tool, target, intent['actor_id'])
        if pr['head']['sha'] != args.expected_head_sha and not (
                claimed and row['commit_sha'] and pr['head']['sha'] == row['commit_sha']):
            raise ConnectorError('The PR head changed. Prepare a new rebase with its exact current head.', dispatched=claimed)
        head_token = (token if head_target == target and not workflow_changes(args)
                      else await self.installation_token(repository=self.target(head_target), write=True,
                                                         workflows=workflow_changes(args)))

        async def verify_head(expected: str) -> dict[str, object]:
            current = await self.authorized_pr(run, token, target, args.number, authority, version, tool)
            if (self.pr_identity(current, target, args.number) != (head_target, branch, base_branch)
                    or current['head']['sha'] != expected or current['base'].get('sha') != args.base_sha):
                raise ConnectorError('The exact PR base or head changed during the rebase.')
            if await self.publication_ref(token, target, base_branch) != args.base_sha:
                raise ConnectorError('The PR target base changed during the rebase.')
            if await self.publication_ref(head_token, head_target, branch) != expected:
                raise ConnectorError('The branch changed externally during the rebase.')
            self.publication_check(run, version, tool, target, intent['actor_id'])
            return current

        if not claimed:
            await self.writable_branch(run, head_token, head_target, branch, version, tool, existing=True, operation=operation)
            await verify_head(args.expected_head_sha)
            repository = await self.request('GET', f'/repositories/{head_target}', token=head_token)
            node_id = repository.get('node_id') if isinstance(repository, dict) else None
            if (not isinstance(repository, dict) or repository.get('id') != head_target
                    or not isinstance(node_id, str) or not 0 < len(node_id) <= 256):
                raise ConnectorError('GitHub did not confirm the rebase head repository identity.', dispatched=False)
            self.publication_check(run, version, tool, target, intent['actor_id'])
            if not row['commit_sha']:
                commit = await self.create_change_commit(run, args, head_token, head_target, version, tool, operation, verify_source=True)
                if commit == '0' * 40:
                    raise ConnectorError('GitHub did not confirm a nonzero prepared rebase commit.')
                self.store.execute("UPDATE github_followups SET commit_sha=? WHERE id=? AND commit_sha=''",
                                   (commit, row['id']))
                self.store.execute("UPDATE github_publication_intents SET phase='prepared' WHERE id=? AND phase='preparing'",
                                   (operation,))
                row = self.store.rows('SELECT * FROM github_followups WHERE id=?', (row['id'],))[0]
            if not re.fullmatch(r'[0-9a-f]{40}', row['commit_sha']) or row['commit_sha'] == '0' * 40:
                raise ConnectorError('The saved rebase commit is invalid. No ref update was dispatched.', dispatched=False)
            await self.writable_branch(run, head_token, head_target, branch, version, tool, existing=True, operation=operation)
            await verify_head(args.expected_head_sha)
            if self.effect_claimed(operation, 'branch'):
                raise ConnectorError('Another broker claimed this rebase. Only read-only recovery is permitted.')
            self.store.execute("UPDATE github_publication_intents SET phase='branch_attempted' WHERE id=? AND phase='prepared'",
                               (operation,))
            response = await self.publication_effect(operation, 'branch', 'POST', '/graphql', token=head_token,
                json={'query': REBASE_REF_MUTATION, 'variables': {'repositoryId': node_id, 'name': 'refs/heads/' + branch,
                                                               'beforeOid': args.expected_head_sha, 'afterOid': row['commit_sha']}})
            data = response.get('data') if isinstance(response, dict) else None
            if (not isinstance(response, dict) or response.get('errors') or not isinstance(data, dict)
                    or not isinstance(data.get('updateRefs'), dict)):
                raise ConnectorError('GitHub did not confirm the atomic rebase. No force update will be repeated.')
        if not row['commit_sha']:
            raise ConnectorError('The claimed rebase has no confirmed prepared commit. No write was repeated.')
        current = await verify_head(row['commit_sha'])
        result = {**self.receipt(authority['result'], target), 'commit': row['commit_sha'],
                  'base_branch': base_branch, 'base_sha': args.base_sha, 'previous_head_sha': args.expected_head_sha,
                  'state': current['state'], 'draft': current['draft'], 'rebased': True,
                  'operation': 'prepared_single_commit_rebase'}
        with self.store.connect() as conn:
            conn.begin_write()
            conn.execute('UPDATE github_followups SET result=? WHERE id=?', (json.dumps(result), row['id']))
            conn.execute("UPDATE github_publication_intents SET phase='complete' WHERE id=?", (operation,))
        return result

    def publication_owns_branch(self, publication, target, version):
        intent = self.publication_intent(publication['id'])
        if intent:
            return self.branch_owned(target, publication['branch'], version)
        expected = f"moyai/{publication['run_id'][:12]}/{publication['id'][:16]}"
        return bool(publication.get('result') and publication['branch'] == expected)

    def confirm_branch(self, row, intent):
        effects = self.store.rows("SELECT response FROM github_publication_effects WHERE publication_id=? AND effect='branch'",
                                  (row['id'],))
        created = (not intent['expected_head_sha'] and effects
                   and confirmed_ref_creation(effects[0]['response'], row['branch'], row['commit_sha']))
        with self.store.connect() as conn:
            conn.begin_write()
            if created:
                conn.execute('''INSERT INTO github_owned_branches
                    (repository_id,branch,connection_version,publication_id,run_id,source_sha)
                    VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING''',
                    (intent['repository_id'], row['branch'], row['connection_version'], row['id'], row['run_id'], intent['source_sha']))
            conn.execute("UPDATE github_publication_intents SET phase='branch_confirmed' WHERE id=?", (row['id'],))

    def publication_pr_receipt(self, pr, row, intent, title):
        target, branch, commit = intent['repository_id'], row['branch'], row['commit_sha']
        if not isinstance(pr, dict):
            raise ConnectorError('The PR response is not an object.')
        head, base = pr.get('head'), pr.get('base')
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ConnectorError('The PR response has no valid head and base.')
        number = pr.get('number')
        if (type(number) is not int or number <= 0
                or pr.get('html_url') != f'https://github.com/{self.repository_name(target)}/pull/{number}'
                or pr.get('draft') is not False or pr.get('state') not in ('open', 'closed')
                or head.get('ref') != branch or head.get('sha') != commit
                or not isinstance(head.get('repo'), dict) or head['repo'].get('id') != target
                or not isinstance(base.get('repo'), dict) or base['repo'].get('id') != target
                or base.get('ref') != intent['base_branch']):
            raise ConnectorError('The PR response does not match the expected publication.')
        return {'number': number, 'url': pr['html_url'], 'branch': branch, 'commit': commit,
                'base_branch': intent['base_branch'], 'draft': False, 'state': pr['state'],
                'repository_id': target, 'repository': self.repository_name(target), 'title': title}

    async def publication_pr(self, run, args, row, intent, token, check):
        target, branch = intent['repository_id'], row['branch']
        prefix = f'/repositories/{target}'

        async def lookup():
            check()
            pulls = await self.request('GET', prefix + '/pulls', token=token,
                params={'state': 'all', 'head': self.repository_name(target).split('/')[0] + ':' + branch,
                        'base': intent['base_branch'], 'per_page': 100})
            check()
            if not isinstance(pulls, list) or len(pulls) > 1:
                raise ConnectorError('Readback did not return a unique PR list.')
            return self.publication_pr_receipt(pulls[0], row, intent, args.title) if pulls else None

        try:
            result = await lookup()
            claimed = self.effect_claimed(row['id'], 'pr')
            if result and not claimed:
                raise ConnectorError('An existing PR is not a confirmed publication of this operation.')
            if result is None:
                if row['attempted'] or claimed:
                    raise ConnectorError('Creation was already attempted; readback has not confirmed the PR.')
                current = await self.publication_ref(token, target, branch)
                if current != row['commit_sha']:
                    raise ConnectorError('The PR head changed before creation.')
                check()
                with self.store.connect() as conn:
                    conn.begin_write()
                    claimed = conn.execute('''INSERT INTO github_publication_effects(publication_id,effect)
                        VALUES(?,'pr') ON CONFLICT DO NOTHING''', (row['id'],)).rowcount
                    if claimed:
                        conn.execute('UPDATE github_publications SET attempted=1 WHERE id=?', (row['id'],))
                        conn.execute("UPDATE github_publication_intents SET phase='pr_attempted' WHERE id=?", (row['id'],))
                if not claimed:
                    raise ConnectorError('Creation was already claimed; read-only recovery is required.')
                try:
                    pr = await self.request('POST', prefix + '/pulls', token=token,
                        json={'title': args.title, 'body': args.body, 'head': branch, 'base': intent['base_branch'],
                              'draft': False, 'maintainer_can_modify': True})
                    result = self.publication_pr_receipt(pr, row, intent, args.title)
                except ConnectorError as creation_error:
                    if 400 <= (getattr(creation_error, 'status_code', 0) or 0) < 500 and creation_error.status_code != 408:
                        self.store.execute('''UPDATE github_publication_effects SET response=?
                            WHERE publication_id=? AND effect='pr' ''', (json.dumps({'rejected': True}), row['id']))
                        raise
                    try:
                        result = await lookup()
                        if result is None:
                            raise ConnectorError('Readback has not confirmed the PR.')
                    except ConnectorError as read_error:
                        raise ConnectorError(f'{creation_error} Read-only recovery: {read_error}') from None
            check()
            return result
        except ConnectorError as error:
            raise ConnectorError(f'Publication unconfirmed for {target}, branch {branch}: {error} '
                                 'No creation retry was sent. Use the same request_key and unchanged arguments '
                                 'for read-only recovery after an attempted creation.') from None

    async def controlled_publication(self, run, args, tool):
        from .github import workflow_changes
        version = run.get('github_connection_version') or self.connection_version()
        self.ensure_publish_allowed(run, version, tool)
        target = await self.selected_target(run, args.repository, args.repository_id)
        self.publication_check(run, version, tool, target)
        identity = hashlib.sha256(f"{run['id']}:{args.request_key}".encode()).hexdigest()
        fingerprint = self.arguments_hash(args, target)
        rows = self.store.rows('SELECT * FROM github_publications WHERE id=?', (identity,))
        if rows:
            row = rows[0]
            self.upgrade_receipt_hash('github_publications', row, args, fingerprint)
            if row['arguments_hash'] != fingerprint:
                raise ConnectorError('That request_key already describes different changes. Use a new key.')
            if row['connection_version'] != version:
                raise ConnectorError('The GitHub installation changed since this publication.')
            if row['result']:
                result = self.receipt(row['result'], target)
                self.record_publication(identity, result, turn_id=run['active_message_id'] or 0)
                return result
            if not self.publication_intent(identity):
                raise ConnectorError('Legacy publication unconfirmed: frozen repository and base metadata are missing. '
                                     'Use github_publication_status for read-only observation; no mutation was retried.')
        files = hasattr(args, 'files')
        token = await self.installation_token(repository=target, write=True, workflows=files and workflow_changes(args))
        self.publication_check(run, version, tool, target)
        intent = self.publication_intent(identity)
        if not intent:
            repo = await self.repository(token, target)
            creates_pr = tool in {'github_create_pull_request', 'github_open_pull_request'}
            base_branch = (getattr(args, 'base_branch', '') or repo['default_branch']) if creates_pr else ''
            branch = getattr(args, 'head_branch', '') or getattr(args, 'branch', '') or f"moyai/{run['id'][:12]}/{identity[:16]}"
            if base_branch == branch and tool in {'github_create_pull_request', 'github_open_pull_request'}:
                raise ConnectorError('The PR base and head must be different branches.')
            source = (args.base_sha if files else args.source_sha if tool == 'github_create_branch' else args.expected_head_sha)
            expected = getattr(args, 'expected_head_sha', '')
            base_sha = (repo['base_sha'] if base_branch == repo['default_branch']
                        else await self.publication_ref(token, target, base_branch)) if creates_pr else source
            if tool == 'github_create_pull_request' and source != base_sha:
                comparison = await self.request('GET', f'/repositories/{target}/compare/{source}...{base_sha}',
                                                 token=token, params={'per_page': 1})
                if comparison.get('status') not in {'ahead', 'identical'}:
                    raise ConnectorError('The checkout base is not on the selected base branch. Select its branch explicitly or prepare a new publication.')
            if tool == 'github_create_branch':
                commit = await self.request('GET', f'/repositories/{target}/git/commits/{source}', token=token)
                if commit.get('sha') != source:
                    raise ConnectorError('GitHub did not confirm the exact source commit.')
            if tool == 'github_publish_branch' and expected and source != expected:
                raise ConnectorError('The expected head must match the checkout base.')
            if tool != 'github_open_pull_request':
                await self.writable_branch(run, token, target, branch, version, tool, existing=bool(expected), operation=identity)
            current = await self.publication_ref(token, target, branch, missing=True)
            if tool == 'github_open_pull_request':
                if not current or current != expected:
                    raise ConnectorError('The existing PR head changed or does not exist.')
            elif expected:
                if not self.branch_owned(target, branch, version):
                    raise ConnectorError('Only a confirmed Moyai-owned branch can be updated.')
                if current != expected:
                    raise ConnectorError('The branch head changed. Check out its current head before publishing.')
            elif current:
                raise ConnectorError('The branch already exists. New publications never overwrite an existing branch.')
            self.publication_check(run, version, tool, target)
            with self.store.connect() as conn:
                conn.begin_write()
                inserted = conn.execute('''INSERT INTO github_publications
                    (id,run_id,message_id,arguments_hash,branch,connection_version,created_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING''',
                    (identity, run['id'], run['active_message_id'] or 0, fingerprint, branch, version, now())).rowcount
                if inserted:
                    conn.execute('''INSERT INTO github_publication_intents
                        (id,repository_id,tool,base_branch,base_sha,source_sha,expected_head_sha,actor_id,
                         head_repository_id,head_branch,connection_version,phase)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (identity, target, tool, base_branch, base_sha, source, expected, run.get('active_user_id', ''),
                         target, branch, version, 'preparing'))
            intent = self.publication_intent(identity)
        row = self.store.rows('SELECT * FROM github_publications WHERE id=?', (identity,))[0]
        if (not intent or row['arguments_hash'] != fingerprint or row['connection_version'] != version
                or intent['repository_id'] != target or intent['tool'] != tool):
            raise ConnectorError('That request_key already describes different changes or a different GitHub connection.')
        check = lambda: self.publication_check(run, version, tool, target, intent['actor_id'])
        check()
        if row['result']:
            return self.receipt(row['result'], target)
        if not row['commit_sha']:
            commit = (await self.create_change_commit(run, args, token, target, version, tool, identity)
                      if files else intent['source_sha'])
            check()
            with self.store.connect() as conn:
                conn.begin_write()
                conn.execute("UPDATE github_publications SET commit_sha=? WHERE id=? AND commit_sha=''", (commit, identity))
                conn.execute("UPDATE github_publication_intents SET phase='prepared' WHERE id=? AND phase='preparing'", (identity,))
            row = self.store.rows('SELECT * FROM github_publications WHERE id=?', (identity,))[0]
        if tool != 'github_open_pull_request':
            await self.publish_ref(run, row, intent, token, check)
        if tool in {'github_create_branch', 'github_publish_branch'}:
            result = {'kind': 'branch', 'repository_id': target, 'repository': self.repository_name(target),
                      'branch': row['branch'], 'commit': row['commit_sha'], 'base_branch': intent['base_branch']}
        else:
            result = await self.publication_pr(run, args, row, intent, token, check)
        if tool != 'github_open_pull_request' and not self.branch_owned(target, row['branch'], version):
            result['branch_ownership_confirmed'] = False
            result['warning'] = ('The branch state matches, but its creation acknowledgment was not confirmed. '
                                 'This receipt does not grant branch write authority; an exact-PR grant is required for further PR head changes.')
        check()
        self.record_publication(identity, result, turn_id=run['active_message_id'] or 0)
        return result

    async def publish_ref(self, run, row, intent, token, check):
        target, branch, expected = intent['repository_id'], row['branch'], intent['expected_head_sha']
        current = await self.publication_ref(token, target, branch, missing=True)
        check()
        claimed = self.effect_claimed(row['id'], 'branch')
        if claimed:
            if current != row['commit_sha']:
                raise ConnectorError('The earlier branch write is unconfirmed or changed externally. No write was repeated.')
        else:
            if (expected and (current != expected or not self.branch_owned(target, branch, row['connection_version']))
                    or not expected and current):
                raise ConnectorError('The branch changed externally. It will not be overwritten.')
            await self.writable_branch(run, token, target, branch, row['connection_version'], intent['tool'],
                                       existing=bool(expected), operation=row['id'])
            current = await self.publication_ref(token, target, branch, missing=True)
            if current != expected:
                raise ConnectorError('The branch changed externally. It will not be overwritten.')
            check()
            self.store.execute("UPDATE github_publication_intents SET phase='branch_attempted' WHERE id=?", (row['id'],))
            path = f'/repositories/{target}/git/refs'
            payload = {'ref': 'refs/heads/' + branch, 'sha': row['commit_sha']}
            if expected:
                path += '/heads/' + quote(branch, safe='/')
                payload = {'sha': row['commit_sha'], 'force': False}
            await self.publication_effect(row['id'], 'branch', 'PATCH' if expected else 'POST', path, token=token, json=payload)
            current = await self.publication_ref(token, target, branch, missing=True)
            check()
            if current != row['commit_sha']:
                raise ConnectorError('GitHub did not confirm the published branch. No write was repeated.')
        self.confirm_branch(row, intent)


def initialize_schema(store):
    store.execute('''CREATE TABLE IF NOT EXISTS github_publication_intents (
        id TEXT PRIMARY KEY, repository_id INTEGER NOT NULL, tool TEXT NOT NULL,
        base_branch TEXT NOT NULL, base_sha TEXT NOT NULL, source_sha TEXT NOT NULL,
        expected_head_sha TEXT NOT NULL DEFAULT '', actor_id TEXT NOT NULL DEFAULT '',
        head_repository_id INTEGER NOT NULL, head_branch TEXT NOT NULL,
        connection_version TEXT NOT NULL, number INTEGER NOT NULL DEFAULT 0,
        phase TEXT NOT NULL)''')
    store.execute('''CREATE TABLE IF NOT EXISTS github_publication_effects (
        publication_id TEXT NOT NULL, effect TEXT NOT NULL, response TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(publication_id,effect))''')
    store.execute('''CREATE TABLE IF NOT EXISTS github_owned_branches (
        repository_id INTEGER NOT NULL, branch TEXT NOT NULL, connection_version TEXT NOT NULL,
        publication_id TEXT NOT NULL, run_id TEXT NOT NULL, source_sha TEXT NOT NULL,
        PRIMARY KEY(repository_id,branch,connection_version))''')
