"""Fixed native stack operations with independently fenced PR authority."""
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agent.github_refs import validate_branch_name
from .connector_errors import ConnectorError
from .github_coding import Repository, clean


API_VERSION = '2026-03-10'
Identifier = Annotated[int, Field(gt=0, strict=True)]
CommitSHA = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$', strict=True)]


class StackRepository(Repository):
    repository_id: Identifier


class StackList(StackRepository):
    pull_request: Identifier | None = None
    page: int = Field(default=1, ge=1, le=10000, strict=True)
    per_page: int = Field(default=30, ge=1, le=100, strict=True)


class StackNumber(StackRepository):
    stack_number: Identifier


class ExpectedPR(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    number: Identifier
    head_repository_id: Identifier
    head_ref: str = Field(min_length=1, max_length=1024)
    head_sha: CommitSHA
    base_repository_id: Identifier
    base_ref: str = Field(min_length=1, max_length=1024)
    base_sha: CommitSHA
    state: Literal['open', 'closed'] = 'open'
    draft: bool = False
    merged_at: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator('head_ref', 'base_ref')
    @classmethod
    def branch(cls, value):
        return validate_branch_name(value)

    @model_validator(mode='after')
    def merged_state(self):
        if self.merged_at is not None and self.state != 'closed':
            raise ValueError('A merged PR must be closed.')
        return self


class StackWrite(StackRepository):
    request_key: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$', strict=True)


class CreateStack(StackWrite):
    pull_requests: list[ExpectedPR] = Field(min_length=2, max_length=100)

    @model_validator(mode='after')
    def unique(self):
        unique_members(self.pull_requests)
        return self


class ExistingStack(StackWrite):
    stack_number: Identifier
    revision: str = Field(pattern=r'^[0-9a-f]{64}$', strict=True)
    members: list[ExpectedPR] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique(self):
        unique_members(self.members)
        return self


class AddToStack(ExistingStack):
    pull_requests: list[ExpectedPR] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def bounded(self):
        unique_members([*self.members, *self.pull_requests])
        if len(self.members) + len(self.pull_requests) > 100:
            raise ValueError('This tool supports at most 100 members in a stack.')
        return self


def unique_members(members):
    if len({member.number for member in members}) != len(members):
        raise ValueError('Select each PR exactly once.')


class ProviderObject(BaseModel):
    model_config = ConfigDict(extra='ignore', strict=True)


class ProviderRepository(ProviderObject):
    id: Identifier


class ProviderRef(ProviderObject):
    ref: str
    sha: CommitSHA
    repo: ProviderRepository


class ProviderPR(ProviderObject):
    number: Identifier
    head: ProviderRef
    base: ProviderRef
    state: Literal['open', 'closed']
    draft: bool
    merged_at: str | None

    def expected(self):
        return ExpectedPR(number=self.number, head_repository_id=self.head.repo.id,
            head_ref=self.head.ref, head_sha=self.head.sha, base_repository_id=self.base.repo.id,
            base_ref=self.base.ref, base_sha=self.base.sha, state=self.state,
            draft=self.draft, merged_at=self.merged_at)


class StackBase(ProviderObject):
    ref: str


class ProviderStack(ProviderObject):
    id: Identifier
    number: Identifier
    node_id: str
    url: str
    base: StackBase
    open: bool
    created_at: str
    pull_requests: list[ProviderPR] = Field(max_length=100)


class MinimalHead(ProviderObject):
    ref: str
    sha: CommitSHA


class MinimalPR(ProviderObject):
    number: Identifier
    state: Literal['open', 'closed']
    draft: bool
    merged_at: str | None
    head: MinimalHead


class MinimalStack(ProviderStack):
    pull_requests: list[MinimalPR] = Field(max_length=100)


READS = {
    'github_stacks': (StackList, 'List native GitHub stacks in a selected repository, optionally filtered by PR. Follow next_page. Use github_stack for a complete write precondition.'),
    'github_stack': (StackNumber, 'Read a native stack and its ordered members/revision. Copy members and revision into add/unstack calls; list summaries do not contain complete PR identities.'),
}
WRITES = {
    'github_create_stack': (CreateStack, 'Group 2–100 existing PRs bottom-to-top. Supply every current head/base/repository. Conservatively requires actual head-branch write authority and writable-branch checks for every affected unmerged PR. Never approves or merges.'),
    'github_add_to_stack': (AddToStack, 'Append new PRs above the stack top. Supply the full current members/revision and new PR identities. Conservatively requires actual head-branch write authority and writable-branch checks for every affected unmerged PR. Never approves or merges.'),
    'github_unstack': (ExistingStack, 'Remove all eligible unmerged PRs from a native stack. No selective removal. Locked/queued PRs can remain: partial_unstack is not dissolution. Conservatively requires actual head-branch write authority and writable-branch checks for every affected unmerged PR. Never clears blockers, approves or merges.'),
}
TOOLS = {**{name: ('github', False, schema, description) for name, (schema, description) in READS.items()},
         **{name: ('github', True, schema, description + ' Use a new request_key for each action; an uncertain dispatch is never repeated.')
            for name, (schema, description) in WRITES.items()}}


def parse_provider(schema, data):
    try:
        return schema.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        raise ConnectorError('GitHub did not return the documented stack or PR identity.', dispatched=False) from None


def stack_snapshot(data, target, number=None):
    stack = parse_provider(ProviderStack, data)
    if number is not None and stack.number != number:
        raise ConnectorError('GitHub returned a different stack.', dispatched=False)
    try:
        members = [member.expected() for member in stack.pull_requests]
        unique_members(members)
    except ValueError:
        raise ConnectorError('GitHub returned invalid stack members.', dispatched=False) from None
    if any(member.base_repository_id != target for member in members):
        raise ConnectorError('The stack contains a PR from another base repository.', dispatched=False)
    snapshot = stack.model_dump()
    snapshot['repository_id'] = target
    revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return stack, members, revision


class GitHubStacks:
    async def stack_call(self, run, name, arguments):
        if name not in TOOLS:
            raise ConnectorError('This stack operation is unavailable.', dispatched=False)
        args = TOOLS[name][2].model_validate(arguments)
        version = run.get('github_connection_version') or self.connection_version()
        target = await self.selected_target(run, args.repository, args.repository_id)
        self.coding_authority(run, name, version, target)
        if name in WRITES:
            async with self.write_lock:
                return await self.stack_write(run, name, args, version, target)
        return await self.stack_read(run, name, args, version, target)

    def stack_gate(self, run, name, version, target, authorities=None):
        self.coding_authority(run, name, version, target)
        if name in WRITES:
            self.ensure_publish_allowed(run, version, name)
        for number, authority in (authorities or {}).items():
            current = self.write_authority(run, target, number, version, name)
            fields = ('id', 'grant', 'branch', 'head_repository_id', 'base_branch')
            if any(current.get(field) != authority.get(field) for field in fields):
                raise ConnectorError('A stack member’s write authority changed.', dispatched=False)

    async def stack_read(self, run, name, args, version, target):
        self.stack_gate(run, name, version, target)
        token = await self.installation_token(repository=target, coding='pull_requests_read')
        self.stack_gate(run, name, version, target)
        prefix = '/repos/' + self.repository_name(target) + '/stacks'
        options = {'params': {'page': args.page, 'per_page': args.per_page,
                   **({'pull_request': args.pull_request} if args.pull_request else {})}} if name == 'github_stacks' else {}
        path = prefix if name == 'github_stacks' else prefix + f'/{args.stack_number}'
        data = await self.request('GET', path, token=token, api_version=API_VERSION, **options)
        self.stack_gate(run, name, version, target)
        result = {'repository_id': target, 'repository': self.repository_name(target), 'untrusted_content': True}
        if name == 'github_stacks':
            if not isinstance(data, list) or len(data) > args.per_page:
                raise ConnectorError('GitHub returned an invalid stack page.', dispatched=False)
            result.update(items=[parse_provider(MinimalStack, item).model_dump() for item in data],
                          next_page=args.page + 1 if len(data) == args.per_page else None)
        else:
            stack, members, revision = stack_snapshot(data, target, args.stack_number)
            result.update(data=stack.model_dump(), members=[member.model_dump() for member in members], revision=revision)
        return clean(result, token)

    async def stack_write(self, run, name, args, version, target):
        members = list(args.pull_requests) if isinstance(args, CreateStack) else list(args.members)
        if isinstance(args, AddToStack):
            members += args.pull_requests
        if any(member.base_repository_id != target for member in members):
            raise ConnectorError('Every stack PR must belong to the selected base repository.', dispatched=False)
        self.stack_gate(run, name, version, target)
        authorities = {member.number: self.write_authority(run, target, member.number, version, name)
                       for member in members if member.merged_at is None}
        self.stack_gate(run, name, version, target, authorities)
        row = self.followup(run, name, args, version, target)
        saved = json.loads(row['result']) if row['result'] else None
        if saved and saved.get('confirmed') is True:
            return saved
        if row['sent']:
            raise ConnectorError('The stack request was dispatched without confirmation. Inspect GitHub; it will not be repeated.',
                                 code='outcome_unknown', dispatched=True)
        token = await self.installation_token(repository=target, coding='pull_requests_write')
        self.stack_gate(run, name, version, target, authorities)
        prefix = '/repos/' + self.repository_name(target) + '/stacks'
        before = None

        async def inspect():
            self.stack_gate(run, name, version, target, authorities)
            current_stack = None
            for member in members:
                self.stack_gate(run, name, version, target, authorities)
                if member in getattr(args, 'pull_requests', []):
                    existing = await self.request('GET', prefix, token=token, api_version=API_VERSION,
                        params={'pull_request': member.number, 'per_page': 100, 'page': 1})
                    self.stack_gate(run, name, version, target, authorities)
                    if existing != []:
                        raise ConnectorError('A new stack member already belongs to a stack or its membership is unconfirmed.',
                                             dispatched=False)
                if member.merged_at is None:
                    pr = await self.authorized_pr(run, token, target, member.number, authorities[member.number],
                        version, name, allow_closed=name == 'github_unstack')
                else:
                    pr = await self.request('GET', f'/repositories/{target}/pulls/{member.number}',
                                            token=token, api_version=API_VERSION)
                self.stack_gate(run, name, version, target, authorities)
                actual = parse_provider(ProviderPR, pr).expected()
                if actual != member:
                    raise ConnectorError('A stack PR’s head, base, repository or state changed.', dispatched=False)
                if actual.merged_at is None:
                    head_target = self.target(actual.head_repository_id)
                    head_token = await self.installation_token(repository=head_target, coding='contents_read')
                    self.stack_gate(run, name, version, target, authorities)
                    await self.writable_branch(run, head_token, head_target, actual.head_ref,
                                               version, name, existing=True)
                    self.stack_gate(run, name, version, target, authorities)
                    current = await self.authorized_pr(run, token, target, member.number, authorities[member.number],
                        version, name, allow_closed=name == 'github_unstack')
                    self.stack_gate(run, name, version, target, authorities)
                    if parse_provider(ProviderPR, current).expected() != actual:
                        raise ConnectorError('A stack PR changed during head-branch policy inspection.', dispatched=False)
            if not isinstance(args, CreateStack):
                data = await self.request('GET', prefix + f'/{args.stack_number}', token=token, api_version=API_VERSION)
                self.stack_gate(run, name, version, target, authorities)
                current_stack, current_members, revision = stack_snapshot(data, target, args.stack_number)
                if revision != args.revision or current_members != args.members:
                    raise ConnectorError('Stack membership or revision changed. Read the stack again.', dispatched=False)
            return current_stack

        before = await inspect()
        if name != 'github_unstack':
            if any(member.merged_at is not None for member in getattr(args, 'pull_requests', [])):
                raise ConnectorError('Only unmerged PRs can be added by this tool.', dispatched=False)
            chain = members if isinstance(args, CreateStack) else [args.members[-1], *args.pull_requests]
            for lower, upper in zip(chain, chain[1:]):
                if (lower.head_repository_id, lower.head_ref) != (upper.base_repository_id, upper.base_ref):
                    raise ConnectorError('Stack members do not form a bottom-to-top base/head chain.', dispatched=False)
        elif not authorities:
            raise ConnectorError('There are no unmerged stack members to remove.', dispatched=False)
        scope = {'repository_id': target, 'repository': self.repository_name(target),
                 'connection_version': version, 'requester': run.get('active_user_id', ''),
                 'members': [member.model_dump() for member in members],
                 'stack_number': getattr(args, 'stack_number', None),
                 'revision': getattr(args, 'revision', None),
                 'authorities': {str(number): {'id': authority['id'], 'grant': bool(authority.get('grant'))}
                                 for number, authority in authorities.items()}}
        prepared = {'operation': name, 'confirmed': False, 'phase': 'prepared', 'scope': scope}
        serialized = json.dumps(prepared, sort_keys=True)
        if saved is not None and saved != prepared:
            raise ConnectorError('The saved stack operation has a different scope.', dispatched=False)
        if saved is None:
            changed = self.store.execute("UPDATE github_followups SET result=? WHERE id=? AND sent=0 AND result=''",
                                         (serialized, row['id']))
            if changed != 1:
                raise ConnectorError('Another caller prepared this stack request. Inspect its receipt.',
                                     code='outcome_unknown', dispatched=True)
        else:
            serialized = row['result']
        await inspect()
        self.stack_gate(run, name, version, target, authorities)
        claimed = self.store.execute('UPDATE github_followups SET sent=1 WHERE id=? AND sent=0 AND result=?',
                                     (row['id'], serialized))
        if claimed != 1:
            raise ConnectorError('Another caller claimed this stack request. No second dispatch was sent.',
                                 code='outcome_unknown', dispatched=True)
        path = prefix if isinstance(args, CreateStack) else prefix + f'/{args.stack_number}/' + (
            'add' if isinstance(args, AddToStack) else 'unstack')
        payload = {} if name == 'github_unstack' else {'pull_requests': [member.number for member in args.pull_requests]}
        try:
            response = await self.request('POST', path, token=token, api_version=API_VERSION,
                                          include_status=True, json=payload)
            status, data = response['status_code'], response['data']
            self.stack_gate(run, name, version, target, authorities)
            if name == 'github_unstack' and status == 204 and data == {}:
                if any(member.merged_at is not None for member in members):
                    raise ValueError('Dissolution did not preserve merged history.')
                outcome, after, remaining = 'dissolved', None, []
            else:
                if status != (201 if isinstance(args, CreateStack) else 200):
                    raise ValueError('Unexpected stack response status.')
                after, remaining, _ = stack_snapshot(data, target, getattr(args, 'stack_number', None))
                if before and after.id != before.id:
                    raise ValueError('Stack identity changed.')
                if name == 'github_unstack':
                    expected = {member.number: member for member in members}
                    remaining_numbers = {member.number for member in remaining}
                    merged_numbers = {member.number for member in members if member.merged_at is not None}
                    if (not remaining or not merged_numbers <= remaining_numbers
                            or any(expected.get(member.number) != member for member in remaining)):
                        raise ValueError('Unstack returned unexpected remaining members.')
                    outcome = 'partial_unstack'
                else:
                    if remaining != members or after.base.ref != members[0].base_ref:
                        raise ValueError('Stack response does not confirm the requested membership.')
                    outcome = 'created' if isinstance(args, CreateStack) else 'added'
            result = clean({'repository_id': target, 'repository': self.repository_name(target), 'operation': name,
                'confirmed': True, 'outcome': outcome, 'status_code': status, 'scope': scope,
                'stack_number': after.number if after else args.stack_number,
                'stack_id': after.id if after else before.id, 'data': after.model_dump() if after else None,
                'remaining_prs': [member.number for member in remaining],
                'removed_prs': [member.number for member in members if member.number not in {item.number for item in remaining}]
                              if name == 'github_unstack' else []}, token)
            changed = self.store.execute('UPDATE github_followups SET result=? WHERE id=? AND sent=1 AND result=?',
                                         (json.dumps(result), row['id'], serialized))
            if changed != 1:
                raise ValueError('Stack confirmation could not be persisted.')
            return result
        except Exception:
            raise ConnectorError('The stack dispatch has no saved confirmation. Inspect GitHub; this request will not be repeated.',
                                 code='outcome_unknown', dispatched=True) from None
