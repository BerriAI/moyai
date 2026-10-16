"""A retired canonical owner reports failure only after serving cleanup returns."""

from types import SimpleNamespace

import pytest
from test_product_deployment import configured
from test_product_deployment import environment as environment_fixture

environment = environment_fixture


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("entrypoint", ["docker", "render"])
def test_native_owner_loss_exit_status_after_server_cleanup(environment, monkeypatch, failed, entrypoint):
    import render_start
    import uvicorn

    from moyai import product_start

    events = []
    life = SimpleNamespace(failed=failed, retire=None)
    application = SimpleNamespace(state=SimpleNamespace(product_lifecycle=life))

    class Server:
        def __init__(self, config, **kwargs):
            self.should_exit = False

        def run(self):
            if failed:
                life.retire()
                assert self.should_exit
                events.append("retired")
            events.append("cleanup_returned")

    monkeypatch.setattr(product_start, "create_application", lambda *args, **kwargs: application)
    monkeypatch.setattr(product_start, "check_runtime_dependencies", dict)
    if entrypoint == "docker":
        environment["MOYAI_PRODUCT_LOCAL_REHEARSAL"] = "true"
        monkeypatch.setattr(product_start, "load_product_config", lambda: configured(environment))
        monkeypatch.setattr(uvicorn, "Server", Server)
        call = lambda: product_start.main(["serve"])
    else:
        monkeypatch.setattr(render_start.os, "environ", {
            **environment, "MOYAI_PUBLIC_URL": "https://package.example.test",
            "RENDER_SERVICE_ID": "srv-synthetic-package", "RENDER_MIGRATION_STAGE": "false",
        })
        monkeypatch.setattr(render_start, "RenderOverlapServer", Server)
        call = render_start.main
    if failed:
        with pytest.raises(SystemExit) as error:
            call()
        assert error.value.code == 1
        assert events == ["retired", "cleanup_returned"]
    else:
        call()
        assert events == ["cleanup_returned"]
