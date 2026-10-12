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
