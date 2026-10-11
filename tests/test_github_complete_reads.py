"""Complete GitHub evidence and stable source identities through real routes."""
import pytest

from test_github_ci import ci
from test_pr_delivery import native_pr
from test_workspace import workspace


def test_job_steps_and_names_are_complete(ci):
    *_, provider, call = ci
    provider.steps = [{'number': i, 'name': 'Detailed step name ' * 150, 'conclusion': 'failure'} for i in range(121)]
    result = call('github_workflow_jobs', run_id=7).json()['jobs'][0]
    assert len(result['steps']) == 121 and not result['steps_truncated']
    assert result['steps'][-1]['name'] == provider.steps[-1]['name']


def test_log_paging_redacts_complete_source_and_rejects_changed_revision(ci):
    *_, provider, call = ci
    provider.log = ('x' * 25000 + '\npassword=sensitive-value\nlast-evidence\n').encode()
    first = call('github_job_logs', job_id=8, max_lines=1).json()
    assert first['text'] == 'x' * 25000 and first['total_lines'] == 3
    second = call('github_job_logs', **first['next_arguments']).json()
    assert second['text'] == 'password=[redacted]'
    last = call('github_job_logs', **second['next_arguments']).json()
    assert last['text'] == 'last-evidence' and last['next_arguments'] is None
    provider.log += b'new evidence\n'
    changed = call('github_job_logs', **first['next_arguments']).json()
    assert changed['code'] == 'source_changed' and 'text' not in changed


def test_log_reads_beyond_former_line_and_page_size_limits(ci):
    *_, provider, call = ci
    provider.log = b'Earlier line\n' * 100001 + b'Final evidence\n' * 501
    result = call('github_job_logs', job_id=8, start_line=100002, max_lines=501).json()
    assert result['text'].splitlines() == ['Final evidence'] * 501
    assert result['total_lines'] == 100502 and result['next_line'] is None


@pytest.mark.parametrize('revision', ['head', 'base'])
def test_pr_pages_reject_revision_changes_before_loading_more_files(native_pr, revision):
    _, client, run_id, url, _, state, calls = native_pr
    state['pr']['base']['sha'] = 'b' * 40
    state['pr']['changed_files'] = 101
    state['files'] = [{'filename': f'{i}.py', 'patch': '+evidence'} for i in range(101)]
    endpoint = f'/api/runs/{run_id}/pull-request'
    first = client.get(endpoint, params={'url': url}).json()
    state['pr'][revision]['sha'] = 'c' * 40
    before = sum(path.endswith('/files') for _, path in calls)
    params = {key: value for key, value in first['next_arguments'].items() if key in {'page', 'head_sha', 'base_sha'}}
    result = client.get(endpoint, params={'url': url, **params})
    assert result.status_code == 409
    assert sum(path.endswith('/files') for _, path in calls) == before


def test_pr_change_during_file_read_never_returns_mixed_evidence(native_pr):
    _, client, run_id, url, _, state, _ = native_pr
    def move(path):
        if path.endswith('/files'):
            state['pr']['head']['sha'] = 'd' * 40
    state['on_request'] = move
    assert client.get(f'/api/runs/{run_id}/pull-request', params={'url': url}).status_code == 409


def test_provider_file_and_patch_omissions_are_explicit(native_pr):
    _, client, run_id, url, _, state, _ = native_pr
    state['pr']['changed_files'] = 301
    state['files'] = [{'filename': f'{i}.png', 'status': 'added'} for i in range(100)]
    endpoint = f'/api/runs/{run_id}/pull-request'
    first = client.get(endpoint, params={'url': url}).json()
    assert first['files'][0]['patch'] is None
    assert first['files'][0]['patch_coverage'] == 'unavailable_from_provider'
    second = client.get(endpoint, params={'url': url, 'page': 2, 'head_sha': first['head']}).json()
    assert second['coverage'] == 'provider_incomplete'
    assert second['remaining_files'] == 201 and second['next_arguments'] is None
