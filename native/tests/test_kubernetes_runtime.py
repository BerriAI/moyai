"""Kubernetes probes must reject a responsive but incorrect role or image."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from moyai import kubernetes_runtime as runtime


@pytest.mark.parametrize('changed', [None, 'X-Moyai-Core', 'X-Moyai-Role', 'X-Moyai-Build', 'X-Moyai-Owner'])
def test_probe_checks_role_release_and_owner(monkeypatch, changed):
    revision = 'a' * 40
    monkeypatch.setenv('MOYAI_PUBLIC_URL', 'https://gateway.example.test/moyai')
    monkeypatch.setenv('MOYAI_BUILD_SHA', revision)
    headers = {'X-Moyai-Core': 'pydantic-v1', 'X-Moyai-Role': 'broker',
               'X-Moyai-Build': revision, 'X-Moyai-Owner': 'active-owner'}
    if changed:
        headers[changed] = '' if changed == 'X-Moyai-Owner' else 'wrong'

    def respond(request, timeout):
        assert request.full_url == 'http://127.0.0.1:8789/health'
        assert request.get_header('Host') == 'gateway.example.test'
        assert timeout == 3
        return nullcontext(SimpleNamespace(status=200, headers=headers))

    monkeypatch.setattr(runtime, 'urlopen', respond)
    assert runtime.healthy('broker') is (changed is None)


def test_probe_failure_closes_readiness_and_marker_removal_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv('MOYAI_PUBLIC_URL', 'https://gateway.example.test/moyai')

    def unavailable(*args, **kwargs):
        raise OSError('connection refused')

    monkeypatch.setattr(runtime, 'urlopen', unavailable)
    assert runtime.healthy('api') is False
    marker = tmp_path / 'ready'
    marker.touch()
    monkeypatch.setattr(runtime, 'MARKER', marker)
    runtime.close_ingress()
    runtime.close_ingress()
    assert not marker.exists()
