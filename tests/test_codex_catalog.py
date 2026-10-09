"""Preserve pinned runtime metadata while making native search reachable."""
import json
import os
from pathlib import Path
import subprocess

import pytest
from codex_cli_bin import bundled_codex_path

from sandbox.codex_catalog import search_catalog


@pytest.mark.parametrize('model', ['gpt-6-astra', 'gpt-6.1-sol'])
def test_search_catalog_preserves_all_bundled_metadata(tmp_path, model):
    env = {'PATH': os.environ['PATH'], 'CODEX_HOME': str(tmp_path / 'native-home')}
    binary = bundled_codex_path()
    original = json.loads(subprocess.check_output(
        [str(binary), 'debug', 'models', '--bundled'], env=env, text=True, timeout=10))
    path = search_catalog(binary, env['CODEX_HOME'], model, env)
    catalog = json.loads(Path(path).read_text())
    selected = next(item for item in original['models'] if item['slug'] == model)
    assert selected['supports_search_tool'] and selected['tool_mode'] == 'code_mode_only'
    selected['tool_mode'] = 'code_mode'
    assert catalog == original


@pytest.mark.parametrize('model,search,mode', [
    ('selected', False, 'code_mode_only'),
    ('selected', True, 'code_mode'),
    ('unknown', True, 'code_mode_only'),
])
def test_search_catalog_leaves_other_models_unchanged(tmp_path, monkeypatch, model, search, mode):
    catalog = {'models': [{'slug': 'selected', 'supports_search_tool': search, 'tool_mode': mode}]}
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, stdout=json.dumps(catalog)))
    assert search_catalog('codex', tmp_path, model, {}) is None
    assert not (tmp_path / 'models.json').exists()
