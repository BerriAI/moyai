"""Finite repository coding API. No merge, approval, generic REST or credential tools."""
import base64
import hashlib
import json
import re
import shlex
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.activity import public_text
from .connector_errors import ConnectorError

PAGE_SIZE = 30
SHA = r'^[0-9a-f]{40}$'
SCOPES = {f'{resource}_{level}': {resource: level} for resource in
          ('issues', 'pull_requests', 'contents', 'actions') for level in ('read', 'write')}
# Contents writes always use the existing publication pipeline, never this API.
SCOPES.pop('contents_write')


class Repository(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository_id: int | None = Field(default=None, gt=0, strict=True)
    repository: str = Field(default='', pattern=r'^(?:[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)?$')


class Page(Repository):
    page: int = Field(default=1, ge=1)


class Search(Page):
    kind: Literal['code', 'issues', 'pull_requests', 'commits']
    query: str = Field(min_length=1, max_length=500,
        description='Plain search terms. Repository scope is fixed by the connection; no qualifiers or boolean operators.')

    @model_validator(mode='after')
    def plain_query(self):
        if any(c in self.query for c in ':\\\r\n') or re.search(r'\b(?:OR|NOT)\b', self.query):
            raise ValueError('Use plain search terms without qualifiers or boolean operators.')
        shlex.split(self.query)
        return self


class Issues(Page):
    state: Literal['open', 'closed', 'all'] = 'open'


class PullRequests(Issues):
    pass


class Number(Repository):
    number: int = Field(gt=0, strict=True)


class Comments(Number):
    page: int = Field(default=1, ge=1)


class Ref(Page):
    ref: str = Field(default='', max_length=1024)


class Commit(Repository):
    sha: str = Field(pattern=SHA)
    page: int = Field(default=1, ge=1)


class Compare(Page):
    base_sha: str = Field(pattern=SHA)
    head_sha: str = Field(pattern=SHA)


class File(Repository):
    path: str = Field(default='', max_length=4096)
    ref: str = Field(pattern=SHA, description='Exact commit SHA; directory and file reads stay on this revision.')

    @model_validator(mode='after')
    def safe_path(self):
        if self.path and (any(p in {'', '.', '..'} for p in self.path.split('/')) or
                          '\\' in self.path or any(ord(c) < 32 for c in self.path)):
            raise ValueError('Use a repository-relative file or directory path.')
        return self


class ReviewThreads(Number):
    cursor: str = Field(default='', max_length=2000)


class Artifacts(Page):
    run_id: int = Field(gt=0, strict=True)


class Artifact(Repository):
    artifact_id: int = Field(gt=0, strict=True)
    offset: int = Field(default=0, ge=0)
    revision: str = Field(default='', pattern=r'^(?:[0-9a-f]{64})?$')


class Write(Repository):
    request_key: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


class PublicationStatus(Write):
    """A request key identifies a receipt, not a new write."""


Name = Annotated[str, Field(min_length=1, max_length=100)]
Body = Annotated[str, Field(min_length=1, pattern=r'\S')]


class CreateIssue(Write):
    title: str = Field(min_length=1, max_length=256)
    body: str = ''
    labels: list[Name] = Field(default_factory=list, max_length=100)
    assignees: list[Name] = Field(default_factory=list, max_length=100)


class UpdateIssue(Write):
    number: int = Field(gt=0, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = None
    state: Literal['open', 'closed'] | None = None
    labels: list[Name] | None = Field(default=None, max_length=100)
    assignees: list[Name] | None = Field(default=None, max_length=100)
    milestone: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode='after')
    def nonempty(self):
        if not any(value is not None for key, value in self.model_dump().items()
                   if key in {'title', 'body', 'state', 'labels', 'assignees', 'milestone'}):
            raise ValueError('Provide at least one issue field to update.')
        return self


class IssueComment(Write):
    number: int = Field(gt=0, strict=True)
    body: Body


class EditIssueComment(IssueComment):
    comment_id: int = Field(gt=0, strict=True)


class PRWrite(Write):
    number: int = Field(gt=0, strict=True)
    head_sha: str = Field(pattern=SHA, description='Current PR head; read the PR before writing.')


class ClosePR(PRWrite):
    pass


class PRDetails(PRWrite):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = None
    labels: list[Name] | None = Field(default=None, max_length=100)
    assignees: list[Name] | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def one_endpoint(self):
        metadata = self.title is not None or self.body is not None
        triage = self.labels is not None or self.assignees is not None
        if metadata == triage:
            raise ValueError('Update title/body OR labels/assignees in one call. Use separate request keys for each.')
        return self


class Reviewers(PRWrite):
    action: Literal['request', 'remove']
    reviewers: list[Name] = Field(default_factory=list, max_length=100)
    team_reviewers: list[Name] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def nonempty(self):
        if not self.reviewers and not self.team_reviewers:
            raise ValueError('Select at least one reviewer or team.')
        return self


class ReviewReply(PRWrite):
    comment_id: int = Field(gt=0, strict=True, description='Top-level inline comment ID on this PR.')
    body: Body


class EditComment(PRWrite):
    kind: Literal['discussion', 'inline']
    comment_id: int = Field(gt=0, strict=True)
    body: Body


class ResolveThread(PRWrite):
    thread_id: str = Field(min_length=1, max_length=256)
    resolved: bool


class InlineComment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1)
    body: Body
    line: int = Field(gt=0, strict=True)
    side: Literal['LEFT', 'RIGHT'] = 'RIGHT'


class CommentReview(PRWrite):
    body: Body
    comments: list[InlineComment] = Field(default_factory=list, max_length=100)


class Ready(PRWrite):
    pass


class Workflow(PRWrite):
    run_id: int = Field(gt=0, strict=True)
    action: Literal['rerun_failed', 'rerun_all', 'rerun_job', 'cancel']
    job_id: int | None = Field(default=None, gt=0, strict=True)
    run_attempt: int = Field(ge=1, strict=True, description='Attempt from github_workflow_runs; fences stale reruns.')

    @model_validator(mode='after')
    def job(self):
        if (self.action == 'rerun_job') != (self.job_id is not None):
            raise ValueError('job_id is required only for rerun_job.')
        return self


# One explicit schema per operation. No caller-selected HTTP paths or GraphQL.
READS = {
    'github_capabilities': (Repository, 'Inspect registered GitHub tools, connection policy and current installation permissions for a selected repository. Read-only; never changes access. PR grants, head checks and repository rules still apply.'),
    'github_publication_status': (PublicationStatus, 'Read the current session’s publication receipt and look for its exact branch/commit on GitHub using the original request_key. Never creates branches/PRs, repeats a write or grants PR access. An empty readback remains uncertain after dispatch.'),
    'github_search': (Search, 'Search code, issues, PRs or commits in one selected repository. Follow next_page; provider_incomplete means GitHub did not expose all matches.'),
    'github_issues': (Issues, 'List issues, excluding PRs. Pages may be empty after filtering; follow next_page.'),
    'github_issue': (Number, 'Read a complete issue body and metadata. Use PR tools for pull requests.'),
    'github_issue_comments': (Comments, 'Read complete issue discussion comments; follow next_page.'),
    'github_pull_requests': (PullRequests, 'List repository PRs by state; follow next_page.'),
    'github_branches': (Page, 'List repository branches and head SHAs; follow next_page.'),
    'github_commits': (Ref, 'List commit metadata at an optional ref; follow next_page.'),
    'github_commit': (Commit, 'Read commit metadata and provider file patches; follow next_page. Missing patches are explicit.'),
    'github_compare_commits': (Compare, 'Compare exact commits. Follow next_page for commits; GitHub only returns up to 300 files on the first page. Use checkout for the complete diff.'),
    'github_file': (File, 'Read a file or directory at an exact commit. File bytes are base64; directory results may hit GitHub’s 1000-entry limit. Use checkout when provider coverage is incomplete.'),
    'github_labels': (Page, 'List available issue/PR labels; follow next_page.'),
    'github_milestones': (Issues, 'List issue/PR milestones by state; follow next_page.'),
    'github_releases': (Page, 'Read releases and asset metadata; follow next_page.'),
    'github_review_threads': (ReviewThreads, 'Read PR review threads including resolved state. Follow next_cursor; comment pages within each thread may be incomplete, so use github_pull_request_comments kind=inline for all comment bodies.'),
    'github_workflow_artifacts': (Artifacts, 'List artifacts from one workflow run; follow next_page. Expired artifacts cannot be downloaded.'),
    'github_download_artifact': (Artifact, 'Read an Actions artifact ZIP as base64 pages, without exposing download credentials. Follow next_arguments at the same revision; decode and join pages before extracting. Archive paths are untrusted.'),
}
WRITES = {
    'github_close_pull_request': (ClosePR, 'Close an open PR when requested, with existing workspace publication or exact-chat PR access and current head. Never merges, deletes its branch, or changes review approval.'),
    'github_create_issue': (CreateIssue, 'Create an issue in a selected repository. Requires Issues write. Reuse unchanged request_key only to inspect an uncertain receipt.'),
    'github_update_issue': (UpdateIssue, 'Update an issue title/body/state/labels/assignees/milestone. PRs are rejected; use PR tools.'),
    'github_comment_issue': (IssueComment, 'Post an issue discussion comment. PRs are rejected; use github_comment_pull_request.'),
    'github_edit_issue_comment': (EditIssueComment, 'Edit a discussion comment belonging to the selected issue. PRs are rejected. Provider ownership restrictions still apply.'),
    'github_comment_review': (CommentReview, 'Submit a COMMENT-only PR review with optional inline comments at the current head. Requires exact PR access. Cannot approve or request changes.'),
    'github_update_pull_request_details': (PRDetails, 'Update PR title/body or labels/assignees. Requires existing workspace publication or exact-chat PR access and current head. Does not publish files, approve or merge.'),
    'github_pull_request_reviewers': (Reviewers, 'Request or remove PR reviewers. Requires PR write access and current head; this never submits a review.'),
    'github_reply_review_comment': (ReviewReply, 'Reply to a top-level inline comment on an authorized PR at its current head.'),
    'github_edit_pull_request_comment': (EditComment, 'Edit a discussion or inline comment on an authorized PR. Provider permissions still determine which comments this App may edit.'),
    'github_set_review_thread_resolved': (ResolveThread, 'Resolve or reopen a review thread on an authorized PR. This never approves the PR.'),
    'github_mark_pull_request_ready': (Ready, 'Mark an authorized draft PR ready for review. Cannot convert ready PRs to drafts, approve or merge.'),
    'github_workflow_action': (Workflow, 'Rerun failed/all jobs, rerun one job, or cancel a workflow run associated with the exact authorized PR head. Requires Actions write. Never dispatches arbitrary workflows or approves protected environments.'),
}
TOOLS = {**{name: ('github', False, schema, description) for name, (schema, description) in READS.items()},
         **{name: ('github', True, schema, description + ' Use a new request_key for a new action. An uncertain dispatch is never automatically repeated.')
            for name, (schema, description) in WRITES.items()}}


def clean(value, token):
    if isinstance(value, str):
        return public_text(value.replace(token, '[redacted]') if token else value, None)
    if isinstance(value, list):
        return [clean(item, token) for item in value]
    if isinstance(value, dict):
        return {key: item if key == 'content' and value.get('encoding') == 'base64' else clean(item, token)
                for key, item in value.items()}
    return value


class GitHubCoding:
    def coding_authority(self, run, name, version, target):
        current = self.store.run(run['id'])
        if (not current or current['deleted_at'] or current['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                or 'github' not in current['plugins'] or not current['token_hash']
                or any(current[key] != run[key] for key in ('token_hash', 'active_message_id', 'active_user_id'))
                or version != self.connection_version() or not self.connectors.allowed(name)):
            raise ConnectorError('GitHub access or the active session changed.', dispatched=False)
        self.target(target)
        if name in WRITES:
            self.ensure_publish_allowed(run, version, name)

    async def coding_call(self, run, name, arguments):
        args = TOOLS[name][2].model_validate(arguments)
        version = run.get('github_connection_version') or self.connection_version()
        target = await self.selected_target(run, args.repository, args.repository_id)
        self.coding_authority(run, name, version, target)
        if name in WRITES:
            async with self.write_lock:
                return await self.coding_write(run, name, args, version, target)
        return await self.coding_read(run, name, args, version, target)

    async def coding_graphql(self, token, query, variables):
        data = await self.request('POST', '/graphql', token=token, json={'query': query, 'variables': variables})
        if data.get('errors') or not isinstance(data.get('data'), dict):
            raise ConnectorError('GitHub did not confirm the review-thread operation. Inspect the PR before retrying a write.')
        return data['data']

    async def review_threads_page(self, token, target, number, cursor=''):
        owner, name = self.repository_name(target).split('/')
        query = '''query($owner:String!,$name:String!,$number:Int!,$cursor:String){
          repository(owner:$owner,name:$name){databaseId pullRequest(number:$number){reviewThreads(first:30,after:$cursor){
            nodes{id isResolved path line comments(first:30){nodes{databaseId body url} pageInfo{hasNextPage endCursor}}}
            pageInfo{hasNextPage endCursor}}}}}'''
        data = await self.coding_graphql(token, query, {'owner': owner, 'name': name, 'number': number, 'cursor': cursor or None})
        repo = data.get('repository') or {}
        if repo.get('databaseId') != target or not repo.get('pullRequest'):
            raise ConnectorError('GitHub did not confirm the selected repository and PR.')
        return repo['pullRequest']['reviewThreads']

    async def coding_read(self, run, name, args, version, target):
        if name in {'github_capabilities', 'github_publication_status'}:
            from .github_diagnostics import read_diagnostic
            return await read_diagnostic(self, run, name, args, version, target)
        scope = ('actions_read' if name in {'github_workflow_artifacts', 'github_download_artifact'} else
                 'issues_read' if name in {'github_issues', 'github_issue', 'github_issue_comments', 'github_labels', 'github_milestones'}
                 or (name == 'github_search' and args.kind == 'issues') else
                 'pull_requests_read' if name in {'github_pull_requests', 'github_review_threads'}
                 or (name == 'github_search' and args.kind == 'pull_requests') else 'contents_read')
        token = await self.installation_token(repository=target, coding=scope)
        check = lambda: self.coding_authority(run, name, version, target)
        prefix = f'/repositories/{target}'
        async def read(path, **params):
            check()
            data = await self.request('GET', path, token=token, params=params)
            check()
            return data
        check()
        result = {'repository_id': target, 'repository': self.repository_name(target), 'untrusted_content': True}
        page = getattr(args, 'page', 1)
        params = {'per_page': PAGE_SIZE, 'page': page}
        if name == 'github_search':
            kind = 'issues' if args.kind == 'pull_requests' else args.kind
            query = ' '.join(json.dumps(term) for term in shlex.split(args.query)) + ' repo:' + self.repository_name(target)
            if kind == 'issues':
                query += ' is:pr' if args.kind == 'pull_requests' else ' is:issue'
            # Divide the provider's 1000-result ceiling exactly; a partial
            # page beyond the ceiling can otherwise be rejected by GitHub.
            data = await read('/search/' + kind, q=query, per_page=25, page=page)
            more = page * 25 < min(data.get('total_count', 0), 1000)
            result.update(data=data, next_page=page + 1 if more else None,
                          provider_incomplete=bool(data.get('incomplete_results') or data.get('total_count', 0) > 1000))
        elif name == 'github_review_threads':
            data = await self.review_threads_page(token, target, args.number, args.cursor)
            result.update(threads=data['nodes'], next_cursor=data['pageInfo']['endCursor'] if data['pageInfo']['hasNextPage'] else None,
                          comments_incomplete=any(n['comments']['pageInfo']['hasNextPage'] for n in data['nodes']))
        elif name == 'github_download_artifact':
            metadata = await read(prefix + f'/actions/artifacts/{args.artifact_id}')
            if metadata.get('expired'):
                raise ConnectorError('This GitHub Actions artifact has expired.', dispatched=False)
            raw = await self.download_job_log(prefix + f'/actions/artifacts/{args.artifact_id}/zip', token, check)
            revision = hashlib.sha256(raw).hexdigest()
            if args.revision and args.revision != revision:
                raise ConnectorError('The artifact changed; restart at offset 0.', code='source_changed', dispatched=False)
            end = min(args.offset + 65536, len(raw))
            result.update(content=base64.b64encode(raw[args.offset:end]).decode(), encoding='base64', total_bytes=len(raw),
                          revision=revision, offset=args.offset, next_arguments={'repository_id': target,
                          'artifact_id': args.artifact_id, 'offset': end, 'revision': revision} if end < len(raw) else None)
        elif name in {'github_issue', 'github_file'}:
            path = f'/issues/{args.number}' if name == 'github_issue' else '/contents/' + quote(args.path, safe='/')
            data = await read(prefix + path, **({'ref': args.ref} if name == 'github_file' else {}))
            if name == 'github_issue' and data.get('pull_request'):
                raise ConnectorError('This number is a PR; use github_pull_request.', dispatched=False)
            incomplete = name == 'github_file' and ((isinstance(data, list) and len(data) >= 1000)
                or (isinstance(data, dict) and data.get('type') == 'file' and data.get('encoding') != 'base64'))
            result.update(data=data, provider_incomplete=incomplete)
        elif name in {'github_commit', 'github_compare_commits'}:
            path = '/commits/' + args.sha if name == 'github_commit' else '/compare/' + args.base_sha + '...' + args.head_sha
            data = await read(prefix + path, **params)
            items = data.get('files' if name == 'github_commit' else 'commits', [])
            commit_limit = name == 'github_commit' and page * PAGE_SIZE >= 3000 and len(items) == PAGE_SIZE
            result.update(data=data, next_page=page + 1 if len(items) == PAGE_SIZE and not commit_limit else None,
                missing_patches=[item.get('filename') for item in data.get('files', []) if 'patch' not in item],
                provider_incomplete=commit_limit if name == 'github_commit' else len(data.get('files', [])) >= 300)
        else:
            paths = {'github_issues': '/issues', 'github_issue_comments': f'/issues/{getattr(args, "number", 0)}/comments',
                     'github_pull_requests': '/pulls', 'github_branches': '/branches', 'github_commits': '/commits',
                     'github_labels': '/labels', 'github_milestones': '/milestones', 'github_releases': '/releases',
                     'github_workflow_artifacts': f'/actions/runs/{getattr(args, "run_id", 0)}/artifacts'}
            if hasattr(args, 'state'):
                params['state'] = args.state
            if name == 'github_commits' and args.ref:
                params['sha'] = args.ref
            data = await read(prefix + paths[name], **params)
            items = data.get('artifacts', []) if name == 'github_workflow_artifacts' else data
            result.update(items=[item for item in items if not item.get('pull_request')] if name == 'github_issues' else items,
                          next_page=page + 1 if len(items) == PAGE_SIZE else None)
        check()
        return clean(result, token)

    async def coding_write(self, run, name, args, version, target):
        check = lambda: self.coding_authority(run, name, version, target)
        check()
        authority = None
        if isinstance(args, PRWrite):
            authority = self.write_authority(run, target, args.number, version, name)
            if authority.get('grant'):
                run = {**run, '_pr_write_scope': (target, args.number, authority['id'])}
        scope = ('actions_write' if isinstance(args, Workflow) else
                 'issues_write' if name in {'github_create_issue', 'github_update_issue', 'github_comment_issue', 'github_edit_issue_comment'} else 'pull_requests_write')
        token = await self.installation_token(repository=target, coding=scope)
        check()
        row = self.followup(run, name, args, version, target)
        # A durable receipt is returned only after current authorization is checked.
        # Never interpret persistence failure following a confirmed write as a retry.
        if row['result']:
            return json.loads(row['result'])
        if row['sent']:
            raise ConnectorError('This request was already dispatched without a saved confirmation. Inspect GitHub before using a new request_key; this call will not repeat it.',
                                 code='outcome_unknown', dispatched=True)
        prefix = f'/repositories/{target}'
        pr_token = token
        if isinstance(args, Workflow):
            pr_token = await self.installation_token(repository=target, coding='pull_requests_read')
        pr = None
        if authority:
            pr = await self.authorized_pr(run, pr_token, target, args.number, authority, version, name)
            if pr['head']['sha'] != args.head_sha:
                raise ConnectorError('The PR head changed. Read the current head and review the operation again.', dispatched=False)
        async def read(path):
            check()
            data = await self.request('GET', prefix + path, token=token)
            check()
            return data
        method, path, payload = 'POST', '', {}
        if isinstance(args, ClosePR):
            method, path, payload = 'PATCH', f'/pulls/{args.number}', {'state': 'closed'}
        elif isinstance(args, CreateIssue):
            path = '/issues'
            payload = {key: getattr(args, key) for key in ('title', 'body', 'labels', 'assignees')}
        elif isinstance(args, (UpdateIssue, IssueComment)):
            issue = await read(f'/issues/{args.number}')
            if issue.get('number') != args.number or issue.get('pull_request'):
                raise ConnectorError('Issue writes cannot target a PR. Use the authorized PR tools.', dispatched=False)
            if isinstance(args, UpdateIssue):
                method, path = 'PATCH', f'/issues/{args.number}'
                payload = {key: value for key, value in args.model_dump().items()
                           if key in {'title', 'body', 'state', 'labels', 'assignees', 'milestone'} and value is not None}
            elif isinstance(args, EditIssueComment):
                comment = await read(f'/issues/comments/{args.comment_id}')
                expected = f'https://api.github.com/repos/{self.repository_name(target)}/issues/{args.number}'
                if comment.get('issue_url') != expected:
                    raise ConnectorError('This comment does not belong to the selected issue.', dispatched=False)
                method, path, payload = 'PATCH', f'/issues/comments/{args.comment_id}', {'body': args.body}
            else:
                path, payload = f'/issues/{args.number}/comments', {'body': args.body}
        elif isinstance(args, PRDetails):
            method = 'PATCH'
            metadata = args.title is not None or args.body is not None
            path = f'/{"pulls" if metadata else "issues"}/{args.number}'
            payload = {key: value for key, value in args.model_dump().items()
                       if key in {'title', 'body', 'labels', 'assignees'} and value is not None}
        elif isinstance(args, Reviewers):
            method = 'POST' if args.action == 'request' else 'DELETE'
            path = f'/pulls/{args.number}/requested_reviewers'
            payload = {'reviewers': args.reviewers, 'team_reviewers': args.team_reviewers}
        elif isinstance(args, (ReviewReply, EditComment)):
            inline = isinstance(args, ReviewReply) or args.kind == 'inline'
            family = 'pulls' if inline else 'issues'
            comment = await read(f'/{family}/comments/{args.comment_id}')
            expected = f'https://api.github.com/repos/{self.repository_name(target)}/{"pulls" if inline else "issues"}/{args.number}'
            if comment.get('pull_request_url' if inline else 'issue_url') != expected:
                raise ConnectorError('This comment does not belong to the authorized PR.', dispatched=False)
            if isinstance(args, ReviewReply):
                if comment.get('in_reply_to_id'):
                    raise ConnectorError('Reply to the thread’s top-level inline comment.', dispatched=False)
                path = f'/pulls/{args.number}/comments/{args.comment_id}/replies'
            else:
                method, path = 'PATCH', f'/{family}/comments/{args.comment_id}'
            payload = {'body': args.body}
        elif isinstance(args, ResolveThread):
            cursor = ''
            while True:
                check()
                threads = await self.review_threads_page(pr_token, target, args.number, cursor)
                if any(thread['id'] == args.thread_id for thread in threads['nodes']):
                    break
                if not threads['pageInfo']['hasNextPage']:
                    raise ConnectorError('This review thread does not belong to the authorized PR.', dispatched=False)
                next_cursor = threads['pageInfo']['endCursor']
                if not next_cursor or next_cursor == cursor:
                    raise ConnectorError('GitHub review-thread pagination did not advance.', dispatched=False)
                cursor = next_cursor
            action = 'resolveReviewThread' if args.resolved else 'unresolveReviewThread'
            payload = {'query': 'mutation($id:ID!){' + action + '(input:{threadId:$id}){thread{id isResolved}}}', 'variables': {'id': args.thread_id}}
            path = '/graphql'
        elif isinstance(args, CommentReview):
            path = f'/pulls/{args.number}/reviews'
            payload = {'event': 'COMMENT', 'commit_id': args.head_sha, 'body': args.body,
                       'comments': [comment.model_dump() for comment in args.comments]}
        elif isinstance(args, Ready):
            if not pr.get('node_id'):
                raise ConnectorError('GitHub did not return the PR node identity.', dispatched=False)
            payload = {'query': 'mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{number isDraft url}}}',
                       'variables': {'id': pr['node_id']}}
            path = '/graphql'
        elif isinstance(args, Workflow):
            workflow = await read(f'/actions/runs/{args.run_id}')
            associated = any(item.get('number') == args.number and item.get('base', {}).get('repo', {}).get('id') == target
                             for item in workflow.get('pull_requests', []))
            # Push-triggered CI often has an empty pull_requests list. Require
            # its exact head repository and branch as well as the commit.
            associated = associated or (not workflow.get('pull_requests') and workflow.get('event') == 'push'
                and workflow.get('head_repository', {}).get('id') == pr['head']['repo']['id']
                and workflow.get('head_branch') == pr['head']['ref'])
            if (workflow.get('head_sha') != args.head_sha or workflow.get('repository', {}).get('id') != target
                    or workflow.get('run_attempt') != args.run_attempt
                    or not associated):
                raise ConnectorError('This workflow run/attempt does not match the authorized PR and head.', dispatched=False)
            if args.action == 'rerun_job':
                job = await read(f'/actions/jobs/{args.job_id}')
                if (job.get('run_id') != args.run_id or job.get('head_sha') != args.head_sha
                        or job.get('run_attempt') != args.run_attempt):
                    raise ConnectorError('This job does not belong to the selected workflow attempt.', dispatched=False)
                path = f'/actions/jobs/{args.job_id}/rerun'
            else:
                suffix = {'rerun_failed': 'rerun-failed-jobs', 'rerun_all': 'rerun', 'cancel': 'cancel'}[args.action]
                path = f'/actions/runs/{args.run_id}/{suffix}'
        else:
            raise ConnectorError('Unsupported coding operation.', dispatched=False)
        # Recheck PR ownership/head after all resource reads and immediately before dispatch.
        if authority:
            current = await self.authorized_pr(run, pr_token, target, args.number, authority, version, name)
            if current['head']['sha'] != args.head_sha:
                raise ConnectorError('The PR head changed before dispatch.', dispatched=False)
        check()
        claimed = self.store.execute("UPDATE github_followups SET sent=1 WHERE id=? AND sent=0 AND result=''", (row['id'],))
        if claimed != 1:
            raise ConnectorError('Another broker already claimed this request. Inspect its receipt before retrying.',
                                 code='outcome_unknown', dispatched=True)
        data = (await self.coding_graphql(token, payload['query'], payload['variables']) if path == '/graphql' else
                await self.request(method, prefix + path, token=token, json=payload))
        result = clean({'repository_id': target, 'repository': self.repository_name(target),
                        'operation': name, 'confirmed': True, 'data': data}, token)
        self.store.execute('UPDATE github_followups SET result=? WHERE id=?', (json.dumps(result), row['id']))
        return result
