
import pytest

from app.config import Settings
from render_start import configure_environment, maintenance


@pytest.fixture(autouse=True)
def restore_render_environment(monkeypatch):
    # configure_environment writes these process-wide values. Register them with
    # monkeypatch so startup tests do not change later tests' Settings defaults.
    import os
    for key in ('PUBLIC_URL', 'TRUST_MODAL_PROXY', 'CHECKPOINT_DIR', 'MODAL_VOLUME_NAME', 'DATA_DIR',
                'MOYAI_WORKER_SHUTDOWN_SECONDS'):
        monkeypatch.setenv(key, os.environ.get(key, ''))
        if not os.environ[key]:
            os.environ.pop(key)










def test_render_uses_its_own_origin_and_disk_not_modal_proxy(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://moyai.example/")
    monkeypatch.setenv("PUBLIC_URL", "https://old.example")
    monkeypatch.setenv("TRUST_MODAL_PROXY", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", "/checkpoints")
    monkeypatch.setenv("MODAL_VOLUME_NAME", "old-volume")
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    configure_environment()
    import os
    assert os.environ["PUBLIC_URL"] == "https://moyai.example"
    assert os.environ["TRUST_MODAL_PROXY"] == "false"
    assert "CHECKPOINT_DIR" not in os.environ and "MODAL_VOLUME_NAME" not in os.environ
    assert os.environ["TRUSTED_PROXY_HOPS"] == "3"


def test_render_lets_the_app_resolve_forwarded_clients(monkeypatch):
    import os

    import render_start
    # main() configures the process environment; keep it from leaking into other tests.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://moyai.example")
    monkeypatch.setenv("RENDER_MIGRATION_STAGE", "true")
    calls = []
    monkeypatch.setattr(render_start.uvicorn.Server, "run", lambda server: calls.append(server.config))
    render_start.main()
    # uvicorn would otherwise rewrite the loopback peer to Render's proxy before the app sees it.
    assert calls[0].proxy_headers is False


def test_staging_cannot_accept_work_or_slack_events():
    from fastapi.testclient import TestClient
    with TestClient(maintenance) as client:
        assert client.get("/health").json()["mode"] == "migration_staging"
        assert client.post("/hooks/slack/events", json={"type":"event_callback"}).status_code == 503
        assert client.post("/api/runs", json={"prompt":"Do work"}).status_code == 503


@pytest.mark.parametrize("name,value", [
    ("MOYAI_MAINTENANCE_TOKEN", "mistyped-token"),
    ("MOYAI_MAINTENANCE_TOKEN", ""),
    ("MOYAI_BUILD_SHA", "not-a-build"),
    ("RENDER_SERVICE_ID", ""),
    ("RENDER_SERVICE_ID", "s" * 201),
    ("MOYAI_RUNTIME_ROLE", "standalone"),
])
def test_invalid_activation_identity_keeps_maintenance_available(monkeypatch, tmp_path, caplog, name, value):
    import os

    from fastapi.testclient import TestClient

    import render_start

    monkeypatch.setattr(os, "environ", {
        "MOYAI_CORE": "pydantic", "MOYAI_PUBLIC_URL": "https://moyai.example",
        "RENDER_MIGRATION_STAGE": "true", "MOYAI_BUILD_SHA": "a" * 40,
        "MOYAI_RUNTIME_ROLE": "api", "RENDER_SERVICE_ID": "srv-local",
        "MOYAI_MAINTENANCE_TOKEN": "c" * 32,
        "MOYAI_MAINTENANCE_SOCKET": str(tmp_path / "control.sock"), name: value,
    })
    monkeypatch.setattr(render_start, "apply_maintenance_activation",
                        lambda: pytest.fail("Invalid identity attempted receipt recovery."))
    def serve(server):
        with TestClient(server.config.app) as client:
            assert client.get("/health").json() == {"status": "ok", "mode": "migration_staging"}
            assert client.get("/").status_code == 503
            assert client.post("/api/runs", json={"prompt": "Do work"}).status_code == 503
    monkeypatch.setattr(render_start.uvicorn.Server, "run", serve)
    render_start.main()
    assert os.environ["RENDER_MIGRATION_STAGE"] == "true"
    assert not (tmp_path / "control.sock").exists()
    assert "mistyped-token" not in caplog.text
    assert "c" * 32 not in caplog.text


def test_valid_staging_identity_does_not_hide_receipt_recovery_errors(monkeypatch):
    import os

    import render_start

    monkeypatch.setattr(os, "environ", {
        "MOYAI_CORE": "pydantic", "MOYAI_PUBLIC_URL": "https://moyai.example",
        "RENDER_MIGRATION_STAGE": "true", "MOYAI_BUILD_SHA": "a" * 40,
        "MOYAI_RUNTIME_ROLE": "api", "RENDER_SERVICE_ID": "srv-local",
        "MOYAI_MAINTENANCE_TOKEN": "c" * 32,
    })
    def reject():
        raise ValueError("Receipt belongs to another staging attempt.")
    monkeypatch.setattr(render_start, "apply_maintenance_activation", reject)
    monkeypatch.setattr(render_start.uvicorn.Server, "run",
                        lambda _: pytest.fail("Unverified receipt started a listener."))
    with pytest.raises(ValueError, match="another staging attempt"):
        render_start.main()


@pytest.mark.parametrize('render_origin', ['', 'https://old.onrender.com'])
def test_explicit_custom_origin_supports_private_render_service(monkeypatch, render_origin):
    import os

    from docker_healthcheck import health_request
    from docker_start import server_command
    monkeypatch.setenv('RENDER_SERVICE_ID', 'srv-test')
    monkeypatch.setenv('RENDER_EXTERNAL_URL', render_origin)
    monkeypatch.setenv('MOYAI_PUBLIC_URL', 'https://moyai.example/')
    monkeypatch.delenv('PORT', raising=False)
    assert Settings(_env_file=None).public_url == 'https://moyai.example/'
    configure_environment()
    assert os.environ['PUBLIC_URL'] == 'https://moyai.example'
    assert server_command()[-1].endswith('render_start.py')
    request = health_request()
    assert request.full_url == 'http://127.0.0.1:10000/health'
    assert request.get_header('Host') == 'moyai.example'


@pytest.mark.parametrize('url', ['http://moyai.example', 'https://user:pass@moyai.example',
                                  'https://moyai.example/path', 'https://moyai.example?bad=1'])
def test_invalid_custom_origins_never_start(monkeypatch, url):
    monkeypatch.setenv('MOYAI_PUBLIC_URL', url)
    with pytest.raises(RuntimeError):
        configure_environment()


@pytest.mark.parametrize('role,seconds', [('api', 240), ('coordinator', 20), ('broker', 240), ('worker', 20), ('standalone', 20)])
def test_render_api_shutdown_budget_leaves_time_for_cleanup(monkeypatch, role, seconds):
    import render_start
    monkeypatch.setenv('MOYAI_PUBLIC_URL', 'https://moyai.example')
    monkeypatch.setenv('MOYAI_RUNTIME_ROLE', role)
    monkeypatch.setenv('RENDER_MIGRATION_STAGE', 'true')
    calls = []
    monkeypatch.setattr(render_start.uvicorn.Server, 'run', lambda server: calls.append(server))
    render_start.main()
    assert calls[0].config.timeout_graceful_shutdown == seconds
    assert calls[0].config.workers == 1
    assert isinstance(calls[0], render_start.RenderOverlapServer) is (role in {'api', 'broker', 'coordinator'})
    if role in {'api', 'broker'}:
        assert calls[0].listener_grace_seconds + seconds == 270
    elif role == 'coordinator':
        assert calls[0].listener_grace_seconds + seconds == 50


@pytest.mark.parametrize('configured,expected', [(None, '240'), ('120', '120'), ('0', '0')])
def test_render_worker_gets_bounded_activity_drain_without_overwriting_explicit_settings(monkeypatch, configured, expected):
    import os
    monkeypatch.setenv('MOYAI_PUBLIC_URL', 'https://moyai.example')
    monkeypatch.setenv('MOYAI_RUNTIME_ROLE', 'worker')
    monkeypatch.delenv('MOYAI_WORKER_SHUTDOWN_SECONDS', raising=False)
    if configured is not None:
        monkeypatch.setenv('MOYAI_WORKER_SHUTDOWN_SECONDS', configured)
    configure_environment()
    assert os.environ['MOYAI_WORKER_SHUTDOWN_SECONDS'] == expected
