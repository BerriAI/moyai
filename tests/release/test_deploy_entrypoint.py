"""Exercise the actual ``python -m`` handler without credentials or network."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('source', ['controller', 'api', 'unexpected'])
def test_module_entrypoint_preserves_shared_safe_errors(tmp_path, source):
    # sitecustomize injects a failure at argument parsing, before credentials or
    # clients are constructed. The running __main__ and imported deploy modules
    # must use the same ReleaseError class in both directions.
    (tmp_path / 'sitecustomize.py').write_text('''
import argparse
import inspect
import urllib.error

def parse_args(*args, **kwargs):
    from scripts.release.deploy import ReleaseError
    source = ''' + repr(source) + '''
    if source == 'controller':
        raise ReleaseError('Synthetic fixed release diagnostic')
    if source == 'unexpected':
        raise RuntimeError('private-diagnostic')
    namespace = inspect.currentframe().f_back.f_globals
    api = namespace['API']('https://synthetic.invalid', 'private-token')
    def failed(*args, **kwargs):
        raise urllib.error.URLError('private-diagnostic')
    api.opener.open = failed
    try:
        api.call('POST', '/synthetic')
    except Exception as error:
        assert isinstance(error, ReleaseError), 'CLI API error lost shared identity'
        raise

argparse.ArgumentParser.parse_args = parse_args
''')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-m', 'scripts.release.deploy', '--release-type', 'rolling'],
        cwd=root, env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': os.pathsep.join((str(tmp_path), str(root)))},
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 1 and not result.stdout
    expected = {
        'controller': 'Synthetic fixed release diagnostic',
        'api': 'Deployment API request failed. A write may have been accepted; inspect the receipt before retrying.',
        'unexpected': 'Unexpected deployment response. Inspect the release receipt; no automatic rollback was attempted.',
    }
    assert result.stderr.strip() == expected[source]
    assert 'private-' not in result.stderr


def test_maintenance_entrypoint_selects_packaged_probe_and_single_shot_migrator(tmp_path, monkeypatch):
    from scripts.release import deploy, maintenance_deploy
    from scripts.release.maintenance_probe import SSHMaintenanceProbe
    from scripts.release.maintenance_migration import SSHMaintenanceMigration
    calls = []
    class Controller:
        def __init__(self, render, github, probe, receipt, **kwargs):
            assert isinstance(probe, SSHMaintenanceProbe)
            assert isinstance(kwargs['offline_migrate'], SSHMaintenanceMigration)
            assert kwargs['preflight_only'] is True
            assert kwargs['api_service_id'] == 'srv-syntheticapi'
            self.record = {'status': 'preflight_passed', 'phase': 'synthetic preflight'}
        def run(self):
            calls.append('maintenance')
    monkeypatch.setattr(maintenance_deploy, 'MaintenanceRelease', Controller)
    for key, value in {'GITHUB_REPOSITORY': 'BerriAI/moyai', 'GITHUB_REF': 'refs/heads/main',
        'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '1', 'RENDER_DEPLOY_SSH_KEY': 'synthetic-key',
        'RENDER_DEPLOY_API_KEY': 'synthetic-render-token', 'GH_TOKEN': 'synthetic-gh-token',
        'MOYAI_API_SERVICE_ID': 'srv-syntheticapi'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    monkeypatch.setattr(sys, 'argv', ['deploy', '--release-type', 'maintenance', '--preflight',
                                    '--receipt', str(tmp_path / 'receipt.json')])
    handlers = {signum: deploy.signal.getsignal(signum)
                for signum in (deploy.signal.SIGINT, deploy.signal.SIGTERM)}
    try:
        deploy.main()
    finally:
        # main normally exits its process. This in-process wiring check must
        # not leave the release cancellation handler installed for other tests.
        for signum, handler in handlers.items():
            deploy.signal.signal(signum, handler)
    assert calls == ['maintenance']
