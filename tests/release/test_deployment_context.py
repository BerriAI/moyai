"""Cross-repository admission uses real Git checkouts, with no deployment keys."""
import json
import subprocess
import sys

import pytest

from scripts.release.context import (
    DEPLOYMENT_REPOSITORY,
    SOURCE_REPOSITORY,
    DeploymentContext,
)
from scripts.release.deploy import GitHub, ReleaseError

SOURCE = 'a' * 40
WORKFLOW = 'b' * 40


@pytest.fixture
def workflow(monkeypatch):
    for name, value in {
        'GITHUB_REPOSITORY': DEPLOYMENT_REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
        'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': WORKFLOW, 'GITHUB_RUN_ID': '123',
    }.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


@pytest.fixture
def checkout(tmp_path):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args],
            check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    (tmp_path / 'app.py').write_text('version = 1\n')
    (tmp_path / '.gitignore').write_text('.venv/\n')
    git('add', '.')
    git('-c', 'user.name=Release test', '-c', 'user.email=release@example.invalid', 'commit', '-m', 'source')
    return tmp_path, git('rev-parse', 'HEAD')


def test_private_workflow_keeps_distinct_source_and_workflow_identities(workflow, checkout):
    root, sha = checkout
    context = DeploymentContext.from_environment(sha)
    context.validate_checkout(root)
    assert context.receipt() == {
        'source': {'repository': SOURCE_REPOSITORY, 'sha': sha},
        'workflow': {'repository': DEPLOYMENT_REPOSITORY, 'sha': WORKFLOW, 'run_id': '123'},
    }


@pytest.mark.parametrize('sha', [None, '', 'main', 'a' * 39, 'A' * 40, '../main', 'a' * 40 + '\n'])
def test_private_workflow_never_falls_back_to_its_own_commit(workflow, sha):
    with pytest.raises(ReleaseError, match='explicit full Moyai source SHA'):
        DeploymentContext.from_environment(sha)


@pytest.mark.parametrize('name,value', [
    ('GITHUB_REPOSITORY', 'someone/moyai-ops'),
    ('GITHUB_REF', 'refs/heads/feature'),
    ('GITHUB_EVENT_NAME', 'pull_request_target'),
    ('GITHUB_SHA', 'main'), ('GITHUB_RUN_ID', '0'),
])
def test_untrusted_or_incomplete_workflow_context_is_rejected(workflow, name, value):
    workflow.setenv(name, value)
    with pytest.raises(ReleaseError):
        DeploymentContext.from_environment(SOURCE)


def test_existing_workflow_keeps_its_dispatch_commit(workflow):
    workflow.setenv('GITHUB_REPOSITORY', SOURCE_REPOSITORY)
    assert DeploymentContext.from_environment().source_sha == WORKFLOW
    assert DeploymentContext.from_environment(WORKFLOW).source_sha == WORKFLOW
    with pytest.raises(ReleaseError, match='own dispatched commit'):
        DeploymentContext.from_environment(SOURCE)


@pytest.mark.parametrize('change', ['wrong_sha', 'tracked_edit', 'untracked_module', 'nested_directory'])
def test_checkout_must_match_the_selected_unmodified_source(workflow, checkout, change):
    root, sha = checkout
    if change == 'wrong_sha':
        sha = SOURCE
    elif change == 'tracked_edit':
        (root / 'app.py').write_text('version = 2\n')
    elif change == 'untracked_module':
        (root / 'sitecustomize.py').write_text('raise RuntimeError("unreviewed")\n')
    else:
        root = root / 'nested'
        root.mkdir()
    with pytest.raises(ReleaseError, match='clean Moyai checkout'):
        DeploymentContext.from_environment(sha).validate_checkout(root)


def test_installed_ignored_dependencies_do_not_dirty_the_checkout(workflow, checkout):
    root, sha = checkout
    (root / '.venv').mkdir()
    (root / '.venv' / 'installed').touch()
    DeploymentContext.from_environment(sha).validate_checkout(root)


def test_check_source_exits_before_credentials_and_api_clients(workflow, checkout, monkeypatch, capsys):
    from scripts.release import deploy
    root, sha = checkout
    monkeypatch.setattr(deploy, 'ROOT', root / 'scripts' / 'release')
    def forbidden(*args, **kwargs):
        pytest.fail('source-only validation must not construct deployment clients')
    monkeypatch.setattr(deploy, 'API', forbidden)
    monkeypatch.delenv('MOYAI_API_SERVICE_ID', raising=False)
    monkeypatch.setattr(sys, 'argv', ['deploy', '--source-sha', sha, '--check-source'])
    deploy.main()
    assert json.loads(capsys.readouterr().out)['source']['sha'] == sha


@pytest.mark.parametrize('status', ['ahead', 'identical', 'behind', 'diverged'])
def test_private_admission_verifies_main_without_replacing_the_pinned_commit(status):
    calls = []
    class API:
        def call(self, method, path):
            calls.append((method, path))
            return {'status': status, 'base_commit': {'sha': SOURCE}, 'merge_base_commit': {'sha': SOURCE}}
    github = GitHub(API(), '123', SOURCE, verify_main=True)
    if status in {'ahead', 'identical'}:
        assert github.candidate() == SOURCE
    else:
        with pytest.raises(ReleaseError, match='not on main'):
            github.candidate()
    assert calls == [('GET', '/repos/BerriAI/moyai/compare/' + SOURCE + '...main')]


@pytest.mark.parametrize('value', [
    {}, {'status': 'ahead'}, {'status': 'ahead', 'base_commit': {'sha': WORKFLOW},
                            'merge_base_commit': {'sha': SOURCE}},
])
def test_incomplete_main_evidence_cannot_admit_a_source_commit(value):
    class API:
        def call(self, *args):
            return value
    with pytest.raises(ReleaseError, match='not on main'):
        GitHub(API(), '123', SOURCE, verify_main=True).candidate()


def test_ci_inventory_does_not_depend_on_the_removed_workflow_endpoint():
    class API:
        def call(self, method, path):
            assert path.startswith('/repos/BerriAI/moyai/actions/runs?')
            return {'workflow_runs': [
                {'head_sha': SOURCE, 'head_branch': 'main', 'event': 'workflow_dispatch',
                 'path': '.github/workflows/deploy-production.yml', 'check_suite_id': 456},
                {'head_sha': SOURCE, 'head_branch': 'main', 'event': 'workflow_dispatch',
                 'path': '.github/workflows/other.yml', 'check_suite_id': 789},
            ]}
    assert GitHub(API(), '123', SOURCE, verify_main=True).deployment_check_suites(SOURCE) == {456}
