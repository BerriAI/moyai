"""Keep Moyai and the pinned Hermes internals importable in one interpreter."""
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox.hermes_compat import prepare_hermes_imports


def test_hermes_imports_preserve_moyai_package_and_initializer(tmp_path):
    vendor = tmp_path / 'agent'
    vendor.mkdir()
    (vendor / '__init__.py').write_text("raise AssertionError('Do not replace the Moyai package')")
    (vendor / 'jiter_preload.py').write_text('preloaded = True\n')
    (vendor / 'hermes_fixture.py').write_text('value = 42\n')
    code = """
import agent
from sandbox.hermes_compat import prepare_hermes_imports
original = agent.__file__
prepare_hermes_imports(SOURCE)
prepare_hermes_imports(SOURCE)
from agent import context_store, hermes_fixture, jiter_preload
assert agent.__file__ == original
assert hermes_fixture.value == 42 and jiter_preload.preloaded
assert agent.__path__.count(str(SOURCE / 'agent')) == 1
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c',
        'from pathlib import Path\nSOURCE = Path(' + repr(str(tmp_path)) + ')\n' + code],
        cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_hermes_module_collision_fails_before_import(tmp_path):
    vendor = tmp_path / 'agent'
    vendor.mkdir()
    (vendor / 'context_store.py').write_text("raise AssertionError('Must not import conflicting module')")
    with pytest.raises(RuntimeError, match='Hermes agent modules conflict with Moyai: context_store'):
        prepare_hermes_imports(tmp_path)
