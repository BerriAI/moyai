from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from test_product_deployment import configured
from test_product_deployment import environment as environment_fixture

from moyai.product_deployment import ProductDeploymentError
from moyai.runtime import Runtime
from moyai.settings import Settings

environment = environment_fixture


def test_direct_runtime_requires_explicit_product_role_before_resources(environment, monkeypatch):
    import moyai.runtime as runtime_module

    def resource_created(*args, **kwargs):
        pytest.fail("Unsupported product ownership constructed a resource.")

    monkeypatch.setattr(runtime_module, "Database", resource_created)
    monkeypatch.setattr(runtime_module, "ObjectStore", resource_created)
    with pytest.raises(ProductDeploymentError, match="explicit"):
        Runtime(Settings(), product_config=replace(configured(environment), role="standalone"))


async def test_login_validation_does_not_reflect_submitted_secret(runtime):
    from moyai.api import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver",
    ) as client:
        secret = "private-password-marker" * 300
        response = await client.post("/api/login", json={"password": secret})
    assert response.status_code == 422
    assert "private-password-marker" not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "password"]


@pytest.mark.parametrize("role", ["api", "coordinator", "broker", "worker", "standalone"])
async def test_distributed_runtime_and_database_share_one_late_bound_drain_service(environment, role):
    environment.update({
        "MOYAI_PRODUCT_LOCAL_REHEARSAL": "true", "MOYAI_RUNTIME_ROLE": role,
        "TEMPORAL_TLS": "false",
    })
    if role == "standalone":
        environment.update(MOYAI_SEPARATE_BROKER="false", MOYAI_COORDINATOR_HANDOFF="false")
    runtime = Runtime(Settings(_env_file=None), product_config=configured(environment))
    try:
        assert runtime.product_drain is runtime.database.product_drain
        if role == "standalone":
            assert runtime.product_drain is None
        else:
            assert runtime.product_drain is not None
            replacement_sessions = object()
            replacement_authority = object()
            runtime.database.sessions = replacement_sessions
            runtime.product.authority = replacement_authority
            assert runtime.product_drain.sessions() is replacement_sessions
            assert runtime.product_drain.authority() is replacement_authority
    finally:
        await runtime.close()
