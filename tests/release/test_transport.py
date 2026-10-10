"""Secret-safe API transport and authenticated read-only SSH invocation."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest

from scripts.release.deploy import API, NoRedirect, ReleaseError, Service, SSHProbe, WORKER
from scripts.release.rehearse import OLD, Render


def test_failed_http_write_is_never_retried_or_logged():
    api = API('https://api.render.com/v1', 'synthetic-secret')
    calls = []
    def failed(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 503, 'synthetic-secret', {}, io.BytesIO(b'secret-body'))
    api.opener.open = failed
    with pytest.raises(ReleaseError) as error:
        api.call('POST', '/services/synthetic/deploys', {'commitId': OLD})
    assert len(calls) == 1
    assert 'synthetic-secret' not in str(error.value)
    assert 'secret-body' not in str(error.value)
    assert 'may have been accepted' in str(error.value)


def test_authenticated_requests_cannot_follow_redirects():
    assert NoRedirect().redirect_request(None, None, 302, '', {}, 'https://different-host') is None


def test_render_inventory_follows_cursor_and_rejects_nonadvancing_pages():
    api = API('https://api.render.com/v1', 'synthetic-secret')
    paths = []
    first = [{'cursor': str(i), 'envVar': {'key': str(i), 'value': 'safe'}} for i in range(100)]
    def page(method, path):
        paths.append(path)
        return first if 'cursor' not in path else [{'cursor': 'done', 'envVar': {'key': 'last', 'value': 'safe'}}]
    api.call = page
    assert len(api.pages('/services/synthetic/env-vars', 'envVar')) == 101
    assert 'cursor=99' in paths[-1]
    api.call = lambda *args: first
    with pytest.raises(ReleaseError, match='incomplete inventory'):
        api.pages('/services/synthetic/env-vars', 'envVar')


def test_ssh_uses_pinned_host_and_only_sends_read_only_probe(monkeypatch):
    env = Render().env[WORKER]
    service = Service(WORKER, 'worker', env, dict(env), OLD, 'dep-synthetic')
    def subprocess_run(args, *, input, text, capture_output, timeout):
        assert 'StrictHostKeyChecking=yes' in args
        assert 'IdentitiesOnly=yes' in args
        assert 'GlobalKnownHostsFile=/dev/null' in args
        assert WORKER + '@ssh.oregon.render.com' in args
        assert 'runuser -u workspace' in args[-1]
        assert all(value not in args[-1] for value in ('do-not-log-session', 'do-not-log-encryption', 'postgresql://'))
        assert input == Path('scripts/release/probe.py').read_text()
        assert timeout == 50 and capture_output
        return SimpleNamespace(returncode=0, stdout='MOYAI_RELEASE_PROBE=' + json.dumps({'ok': True}))
    monkeypatch.setattr('scripts.release.deploy.subprocess.run', subprocess_run)
    assert SSHProbe(Path('/synthetic/key'))(service) == {'ok': True}
