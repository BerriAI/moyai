"""The rehearsal adapter cannot redirect its database observation off loopback."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.release import local_probe


@pytest.fixture
def environment():
    return {
        'MOYAI_PRODUCT_LOCAL_REHEARSAL': 'true',
        'MOYAI_DATABASE_URL': 'postgresql://fixture:private-pw@127.0.0.1:55297/disposable',
        'TEMPORAL_ADDRESS': '127.0.0.1:55323',
        'OBJECT_STORAGE_ENDPOINT': 'http://127.0.0.1:55333',
        'MOYAI_PUBLIC_URL': 'http://127.0.0.1:55402',
    }


@pytest.mark.parametrize('field,value', [
    ('MOYAI_PRODUCT_LOCAL_REHEARSAL', 'false'),
    ('MOYAI_DATABASE_URL', 'postgresql://private-pw@production.invalid/prod'),
    ('MOYAI_DATABASE_URL', 'postgresql://localhost/disposable?host=production.invalid'),
    ('MOYAI_DATABASE_URL', 'postgresql://localhost'),
    ('TEMPORAL_ADDRESS', 'production.invalid:7233'),
    ('OBJECT_STORAGE_ENDPOINT', 'https://bucket.example.com'),
    ('MOYAI_PUBLIC_URL', 'https://production.invalid'),
    ('RENDER_SERVICE_ID', 'srv-fixture'),
])
def test_rejected_scope_never_starts_a_child_or_prints_configuration(environment, monkeypatch, capsys, field, value):
    environment[field] = value
    monkeypatch.setattr(local_probe.subprocess, 'run', lambda *args, **kwargs: pytest.fail('Invalid scope executed'))
    with pytest.raises(ValueError) as error:
        local_probe.observe(SimpleNamespace(), checkout='/synthetic', python='/synthetic/python',
                            port=55404, environment=environment)
    assert 'private-pw' not in str(error.value) and capsys.readouterr() == ('', '')


def test_loopback_adapter_keeps_the_installed_source_probe_and_preserved_environment(environment, monkeypatch):
    calls = []
    options = {'sha': 'a' * 40, 'role': 'api', 'staged': False, 'drain': False}
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr='')
    monkeypatch.setattr(local_probe.subprocess, 'run', run)
    assert local_probe.observe(SimpleNamespace(probe_options=lambda: options),
        checkout=Path('/synthetic/source'), python=Path('/synthetic/legacy/python'),
        port=55404, environment=environment) == {'ok': True}
    argv, kwargs = calls[0]
    assert argv[:2] == ['/synthetic/legacy/python', '-'] and json.loads(argv[2]) == options
    assert kwargs['env'] == environment and kwargs['timeout'] == 60
    assert '55404' in kwargs['input'] and '_namespace["result"]' in kwargs['input']
    assert 'private-pw' not in kwargs['input'] and 'private-pw' not in argv[-1]
