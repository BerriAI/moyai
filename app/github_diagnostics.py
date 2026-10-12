"""Read-only GitHub capability and publication diagnostics. No recovery writes."""
import hashlib
import json

from .connector_errors import ConnectorError
from .github_coding import READS, WRITES, clean


def required_permissions(name):
    if name == 'github_capabilities':
        return {}
    if name == 'github_ci_checks':
        return {'checks': 'read', 'statuses': 'read'}
    if name == 'github_workflow_action':
        return {'actions': 'write', 'pull_requests': 'read'}
    if name in {'github_workflow_runs', 'github_workflow_jobs', 'github_job_logs',
                'github_workflow_artifacts', 'github_download_artifact'}:
        return {'actions': 'read'}
    if name == 'github_update_ruleset_reviewers':
        return {'administration': 'write'}
    if name in {'github_repositories', 'github_rulesets', 'github_ruleset'}:
        return {'metadata': 'read'}
    if name in {'github_create_pull_request', 'github_update_pull_request'}:
        return {'contents': 'write', 'pull_requests': 'write'}
    if name in {'github_comment_pull_request', 'github_request_pull_request_write_access'}:
        return {'contents': 'write', 'pull_requests': 'write'}
    if name in WRITES:
        return {'issues' if 'issue' in name and 'pull_request' not in name else 'pull_requests': 'write'}
    if name in {'github_issues', 'github_issue', 'github_issue_comments', 'github_labels', 'github_milestones'}:
        return {'issues': 'read'}
    if name in {'github_pull_requests', 'github_review_threads', 'github_publication_status'}:
        return {'pull_requests': 'read'}
    if name in READS and name != 'github_search':
        return {'contents': 'read'}
    # These existing readers mint the combined code/PR read token. Search varies
    # by kind, so its conditional requirements are reported separately below.
    return {'contents': 'read', 'pull_requests': 'read'}


async def read_diagnostic(github, run, name, args, version, target):
    from .github import TOOLS, supports_permissions
    check = lambda: github.coding_authority(run, name, version, target)
    check()
    result = {'repository_id': target, 'repository': github.repository_name(target)}
    if name == 'github_capabilities':
        credentials = github.saved_credentials()
        installed = await github.request('GET', f'/app/installations/{credentials["installation_id"]}',
                                         token=github.app_jwt())
        check()
        account = installed.get('account') or {}
        if account.get('type') != 'Organization' or account.get('id') != credentials.get('account_id'):
            raise ConnectorError('The installation belongs to a different organization.', dispatched=False)
        permissions = installed.get('permissions') or {}
        active = not bool(installed.get('suspended_at'))
        entries = []
        for tool in sorted(TOOLS):
            required = required_permissions(tool)
            conditional = ({'code': {'contents': 'read'}, 'commits': {'contents': 'read'},
                            'issues': {'issues': 'read'}, 'pull_requests': {'pull_requests': 'read'}}
                           if tool == 'github_search' else {})
            # Search has no single permission requirement; report each kind.
            if conditional:
                required = {}
            missing = {key: level for key, level in required.items()
                       if not supports_permissions(permissions, {key: level})}
            entries.append({'tool': tool, 'write': TOOLS[tool][1],
                'policy_allowed': github.connectors.allowed(tool), 'required_permissions': required,
                'missing_permissions': missing, 'installation_active': active,
                **({'requirements_by_kind': conditional} if conditional else {})})
        return {**result, 'tools': entries, 'installation_active': active,
            'workflow_file_permission': {'required': {'workflows': 'write'},
                                        'installed': supports_permissions(permissions, {'workflows': 'write'})},
            'prohibited_operations': ['merge', 'auto_merge', 'approve_review'],
            'limitations': 'Registration and installation permissions do not prove execution will succeed. '
                          'Exact PR grants, current heads, repository rules and token issuance still apply. '
                          'Only an organization owner can change installation permissions.'}

    identity = hashlib.sha256(f"{run['id']}:{args.request_key}".encode()).hexdigest()
    rows = github.store.rows('SELECT * FROM github_publications WHERE id=? AND run_id=?', (identity, run['id']))
    if not rows:
        return {**result, 'status': 'not_recorded', 'instruction': 'No publication receipt exists for this key in this session.'}
    row = rows[0]
    if row['connection_version'] != version:
        raise ConnectorError('The GitHub connection changed since this publication.', dispatched=False)
    if row['result']:
        if json.loads(row['result']).get('repository_id') != target:
            raise ConnectorError('This receipt belongs to a different repository.', dispatched=False)
        saved = github.receipt(row['result'], target)
        return clean({**result, 'status': 'confirmed_receipt', 'receipt': saved,
                      'instruction': 'Historical confirmation; use github_pull_request for current PR state.'}, '')
    if not row['commit_sha']:
        return {**result, 'status': 'preparation_unconfirmed',
                'instruction': 'No prepared commit or PR confirmation was saved. This tool performs no publication.'}
    token = await github.installation_token(repository=target, coding='pull_requests_read')
    check()
    pulls = await github.request('GET', f'/repositories/{target}/pulls', token=token,
        params={'state': 'all', 'head': github.repository_name(target).split('/')[0] + ':' + row['branch'], 'per_page': 100})
    check()
    if not isinstance(pulls, list) or len(pulls) > 1:
        raise ConnectorError('Readback did not return a unique PR list.', dispatched=False)
    observed = None
    if pulls:
        pr = pulls[0]
        number = pr.get('number') if isinstance(pr, dict) else None
        head, base = (pr.get('head') or {}, pr.get('base') or {}) if isinstance(pr, dict) else ({}, {})
        def repository_id(ref):
            repo = ref.get('repo') if isinstance(ref, dict) else None
            return repo.get('id') if isinstance(repo, dict) else None
        if (type(number) is not int or number <= 0 or not isinstance(head, dict) or not isinstance(base, dict)
                or repository_id(head) != target or repository_id(base) != target
                or head.get('ref') != row['branch'] or head.get('sha') != row['commit_sha']
                or pr.get('html_url') != f'https://github.com/{github.repository_name(target)}/pull/{number}'
                or pr.get('state') not in {'open', 'closed'} or type(pr.get('draft')) is not bool):
            raise ConnectorError('Readback does not match the recorded branch and commit.', dispatched=False)
        observed = {key: pr[key] for key in ('number', 'html_url', 'state', 'draft')}
    # Legacy unconfirmed rows have no repository/base field. Do not turn a
    # caller-selected destination into a saved receipt or new write authority.
    return clean({**result, 'status': 'observed_match' if observed else 'unconfirmed',
        'branch': row['branch'], 'commit': row['commit_sha'], 'creation_attempted': bool(row['attempted']),
        'observed_pull_request': observed, 'receipt_saved': False,
        'instruction': 'This is a read-only observation in the selected repository, not a publication receipt or write grant. '
                       'Read the observed PR to verify its current state; do not submit another publication to check status. '
                       'Never use an empty readback as permission to repeat an uncertain write.'}, token)
