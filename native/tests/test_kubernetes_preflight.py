"""The installation checks are read-only and run before the first claim."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml

from moyai.deployment_config import InstallationError
from moyai.kubernetes_preflight import check_workspace, validate_template
from moyai.settings import Settings

SAMPLE = Path(__file__).resolve().parents[2] / "charts/moyai/examples/workspace-pool.yaml"


def template_and_pool():
    return list(yaml.safe_load_all(SAMPLE.read_text()))


def configured(**changes):
    arguments = {
        "sandbox_provider": "kubernetes", "sandbox_installation_id": "test",
        "sandbox_namespace": "workspaces", "sandbox_template": "session-runtime", "sandbox_warm_pool": "session-pool",
        "sandbox_router_url": "https://router.example.test", "sandbox_router_token": "workspace-test-token",
    }
    return Settings(_env_file=None, **(arguments | changes))


@pytest.mark.parametrize("patch", [
    {"sandbox_namespace": "invalid scope"}, {"sandbox_warm_pool": "-not-a-name"},
    {"sandbox_template": ""}, {"sandbox_router_url": "http://router.routing.svc"},
    {"sandbox_router_url": "https://user:secret@host"}, {"sandbox_router_url": "https://host/x"},
    {"sandbox_router_url": "https://host:65536"}, {"sandbox_router_url": "https://host?key"},
    {"sandbox_router_token": ""}, {"sandbox_router_token_file": "/second/source"},
    {"sandbox_router_token": "", "sandbox_router_token_file": "relative"},
])
def test_config_rejects_invalid_workspace_settings(patch):
    with pytest.raises(ValueError):
        configured(**patch).validate_kubernetes_workspace()


def test_projected_router_token_is_reread_after_rotation(tmp_path):
    path = tmp_path / "token"
    settings = configured(sandbox_router_token="", sandbox_router_token_file=str(path))
    settings.validate_kubernetes_workspace()
    for token in ("first", "rotated"):
        path.write_text(token + "\n")
        assert settings.read_sandbox_router_token() == token


@pytest.mark.parametrize("change", [
    "pool_target", "warm_members", "allow_override", "managed_network", "automount", "foreign_volume",
    "missing_digest", "missing_mount", "shared_pvc", "injected_secret", "credential_projection",
])
def test_template_rejects_invalid_runtime_or_credential_access(change):
    template, pool = template_and_pool()
    pod = template["spec"]["podTemplate"]["spec"]
    container = pod["containers"][0]
    if change == "pool_target":
        pool["spec"]["sandboxTemplateRef"]["name"] = "other"
    elif change == "warm_members":
        pool["spec"]["replicas"] = 1
    elif change == "allow_override":
        template["spec"]["envVarsInjectionPolicy"] = "Overrides"
    elif change == "managed_network":
        del template["spec"]["networkPolicyManagement"]
    elif change == "automount":
        del pod["automountServiceAccountToken"]
    elif change == "foreign_volume":
        container["volumeMounts"][0]["name"] = "other"
    elif change == "missing_digest":
        container["args"] = []
    elif change == "missing_mount":
        container["volumeMounts"] = []
    elif change == "shared_pvc":
        pod["volumes"] = [{"name": "workspace", "persistentVolumeClaim": {"claimName": "shared"}}]
    elif change == "injected_secret":
        container["envFrom"] = [{"secretRef": {"name": "private"}}]
    else:
        pod["volumes"] = [{"name": "api", "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}}]
    with pytest.raises(InstallationError):
        validate_template(pool, template, "session-runtime")


@pytest.mark.asyncio
async def test_preflight_verifies_pool_permissions_and_private_router(monkeypatch):
    from kubernetes_asyncio import client, config

    template, pool = template_and_pool()
    settings = configured()
    get = AsyncMock(side_effect=[pool, template])
    review = AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(allowed=True)))
    exec_process = AsyncMock(return_value=SimpleNamespace(wait=AsyncMock(return_value=0), returncode=0))
    monkeypatch.setattr("moyai.kubernetes_preflight.asyncio.create_subprocess_exec", exec_process)
    monkeypatch.setattr(config, "load_incluster_config", Mock())
    monkeypatch.setattr(client, "ApiClient", Mock(return_value=AsyncMock()))
    monkeypatch.setattr(client, "CustomObjectsApi", Mock(return_value=SimpleNamespace(get_namespaced_custom_object=get)))
    monkeypatch.setattr(client, "AuthorizationV1Api", Mock(return_value=SimpleNamespace(create_self_subject_access_review=review)))

    def health(request):
        assert request.url == "https://router.example.test/healthz"
        assert "authorization" not in request.headers
        assert request.headers["user-agent"] == "workspace-preflight"
        return httpx.Response(200, json={"status": "ok"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(health), **kwargs))
    await check_workspace(settings)
    assert [call.args[-2:] for call in get.await_args_list] == [
        ("sandboxwarmpools", "session-pool"), ("sandboxtemplates", "session-runtime"),
    ]
    permissions = [call.kwargs["body"].spec.resource_attributes for call in review.await_args_list]
    assert all(item.namespace == "workspaces" for item in permissions)
    assert {(item.resource, item.verb) for item in permissions} == {
        ("sandboxclaims", "get"), ("sandboxclaims", "watch"), ("sandboxclaims", "create"),
        ("sandboxclaims", "patch"), ("sandboxclaims", "delete"), ("sandboxes", "get"),
        ("persistentvolumeclaims", "get"),
    }
