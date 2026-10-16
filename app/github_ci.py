"""Repository-scoped CI metadata and complete, revision-pinned log pages."""
import hashlib
import re
from urllib.parse import urlsplit

import httpx

from app.activity_text import public_text
from .connector_errors import ConnectorError


PAGE_SIZE = 30


def fields(value, names):
    return {key: public_text(value[key], None) if isinstance(value[key], str) else value[key]
            for key in names if key in value}


def log_destination(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and not parsed.fragment
            and (host.endswith('.blob.core.windows.net') or host.endswith('.actions.githubusercontent.com')
                 or host == 'objects.githubusercontent.com'))


class GitHubCI:
    def ensure_ci_allowed(self, name, version, target):
        if not self.connectors.allowed(name) or version != self.connection_version():
            raise ConnectorError('GitHub access changed. Read CI again after checking the connection.')
        self.target(target)

    async def read_ci(self, run, name, args):
        version = run.get('github_connection_version') or self.connection_version()
        target = await self.selected_target(run, args.repository, args.repository_id)
        self.ensure_ci_allowed(name, version, target)
        token = await self.installation_token(repository=target, ci='checks' if name == 'github_ci_checks' else 'actions')
        self.ensure_ci_allowed(name, version, target)
        prefix = f'/repositories/{target}'

        async def read(path, **params):
            self.ensure_ci_allowed(name, version, target)
            result = await self.request('GET', prefix + path, token=token, params=params)
            self.ensure_ci_allowed(name, version, target)
            return result

        result = {'repository_id': target, 'repository': self.repository_name(target), 'untrusted_content': True}
        if name == 'github_ci_checks':
            checks = await read(f'/commits/{args.head_sha}/check-runs', per_page=PAGE_SIZE, page=args.page, filter='latest')
            statuses = await read(f'/commits/{args.head_sha}/statuses', per_page=PAGE_SIZE, page=args.page)
            items = checks.get('check_runs', [])
            result.update(head_sha=args.head_sha,
                          check_runs=[fields(item, ('id', 'name', 'status', 'conclusion', 'head_sha', 'details_url',
                                                    'started_at', 'completed_at')) for item in items],
                          statuses=[fields(item, ('id', 'context', 'state', 'description', 'target_url', 'created_at'))
                                    for item in statuses],
                          next_page=args.page + 1 if len(items) == PAGE_SIZE or len(statuses) == PAGE_SIZE else None)
        elif name == 'github_workflow_runs':
            params = {'per_page': PAGE_SIZE, 'page': args.page}
            if args.head_sha:
                params['head_sha'] = args.head_sha
            if args.branch:
                params['branch'] = args.branch
            runs = await read('/actions/runs', **params)
            items = runs.get('workflow_runs', [])
            result.update(workflow_runs=[fields(item, ('id', 'name', 'event', 'status', 'conclusion', 'head_sha',
                'head_branch', 'run_attempt', 'html_url', 'created_at', 'updated_at')) for item in items],
                next_page=args.page + 1 if len(items) == PAGE_SIZE else None)
        elif name == 'github_workflow_jobs':
            jobs = await read(f'/actions/runs/{args.run_id}/jobs', per_page=PAGE_SIZE, page=args.page, filter='latest')
            items = jobs.get('jobs', [])
            result.update(run_id=args.run_id, jobs=[{
                **fields(item, ('id', 'run_id', 'run_attempt', 'head_sha', 'name', 'status', 'conclusion', 'html_url',
                                'started_at', 'completed_at')),
                'steps': [fields(step, ('number', 'name', 'status', 'conclusion', 'started_at', 'completed_at'))
                          for step in item.get('steps', [])],
                'steps_truncated': False} for item in items],
                next_page=args.page + 1 if len(items) == PAGE_SIZE else None)
        else:
            raw = await self.download_job_log(prefix + f'/actions/jobs/{args.job_id}/logs', token,
                lambda: self.ensure_ci_allowed(name, version, target))
            revision = hashlib.sha256(raw).hexdigest()
            if args.revision and args.revision != revision:
                raise ConnectorError('The job log changed. Read again from line 1 before combining evidence.',
                                     code='source_changed', dispatched=False, retryable=True)
            text = raw.decode('utf-8', errors='replace')
            text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
            # The token minted for this request must never appear in a result,
            # even if an upstream log unexpectedly echoes it without a label.
            text = public_text(text.replace(token, '[redacted]'), None)
            lines = text.splitlines()
            start = args.start_line - 1
            selected = lines[start:start + args.max_lines]
            end = start + len(selected)
            next_line = end + 1 if end < len(lines) else None
            result.update(job_id=args.job_id, start_line=args.start_line, text='\n'.join(selected),
                          next_line=next_line, revision=revision, total_lines=len(lines),
                          next_arguments={'repository_id': target, 'job_id': args.job_id,
                              'start_line': next_line, 'max_lines': args.max_lines, 'revision': revision} if next_line else None,
                          download_truncated=False, truncated=bool(next_line),
                          coverage='page' if next_line else 'final_page')
        self.ensure_ci_allowed(name, version, target)
        return result

    async def download_job_log(self, path, token, check_access):
        # Only GitHub constructs the temporary destination. Never send the
        # installation Authorization header or API cookies to blob storage.
        url = 'https://api.github.com' + path
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                for attempt in range(2):
                    check_access()
                    async with client.stream('GET', url, headers=headers) as response:
                        if attempt == 0 and response.status_code == 302:
                            location = response.headers.get('location', '')
                            if not log_destination(location):
                                raise ConnectorError('GitHub returned an unexpected job-log destination.')
                            url, headers = location, {}
                            client.cookies.clear()
                            continue
                        if response.status_code != 200:
                            raise ConnectorError(f'GitHub job logs are unavailable ({response.status_code}). '
                                                 'Check whether the job has finished, logs have expired, and Actions read access is enabled.')
                        raw = bytearray()
                        async for block in response.aiter_bytes():
                            check_access()
                            raw.extend(block)
                        return bytes(raw)
        except (httpx.HTTPError, ValueError):
            raise ConnectorError('Could not read GitHub job logs. Try the read again; no action was performed.') from None
        raise ConnectorError('GitHub did not return job logs.')
