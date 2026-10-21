from __future__ import annotations

import argparse
import json
import shutil
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from moyai import deployment
from moyai.deployment_config import (
    InstallationError,
    configuration,
    new_state,
    read_state,
    standalone_lock,
    write_state,
)
from moyai.product_config import load_product_config
from moyai.product_deployment import require_startable


@pytest.fixture
def install_environment(tmp_path, monkeypatch):
    @asynccontextmanager
    async def lock(values):
        yield
    monkeypatch.setattr(deployment.deployment_bootstrap, "installation_lock", lock)
    return {
        "DATA_DIR": str(tmp_path),
        "MOYAI_INSTALLATION_ID": "test",
        "MOYAI_INSTALL_TOPOLOGY": "standalone",
        "MOYAI_BUILD_SHA": "1" * 40,
        "MOYAI_DATABASE_URL": "postgresql://user:private-password@localhost:5432/moyai",
        "OBJECT_STORAGE_BUCKET": "private-installation",
        "OBJECT_STORAGE_REGION": "us-east-1",
        "MOYAI_PUBLIC_URL": "https://workspace.example.test",
        "TEMPORAL_ADDRESS": "namespace.example.test:7233",
        "TEMPORAL_NAMESPACE": "installation",
        "TEMPORAL_API_KEY": "private-temporal",
        "TEMPORAL_TLS": "true",
        "LITELLM_API_BASE": "https://gateway.example.test/v1",
        "LITELLM_API_KEY": "private-inference",
        "AGENT_MODEL": "test/model",
        "MODAL_TOKEN_ID": "private-modal-id",
        "MODAL_TOKEN_SECRET": "private-modal-secret",
    }


def prepared(environment):
    values = configuration(environment, None)
    state = new_state(environment, values)
    return state, configuration(environment, state)


def test_lambda_configuration_preserves_native_provider_without_modal_credentials(install_environment):
    install_environment.pop("MODAL_TOKEN_ID")
    install_environment.pop("MODAL_TOKEN_SECRET")
    install_environment.update(
        SANDBOX_PROVIDER="lambda", LAMBDA_REGION="us-east-1", LAMBDA_IMAGE="native-image",
        LAMBDA_IMAGE_VERSION="1.0", LAMBDA_CHECKPOINT_BUCKET="private-checkpoints",
        SNAPSHOT_TIMEOUT_SECONDS="900",
    )
    _, values = prepared(install_environment)
    from moyai.deployment_checks import validate
    configured = validate(values)
    assert configured.sandbox_provider == "lambda"
    assert configured.lambda_checkpoint_bucket == "private-checkpoints"
    assert configured.lambda_checkpoint_timeout == 900


@pytest.mark.parametrize("runtime", [None, "gvisor", "vm"])
def test_installer_preserves_modal_runtime_for_every_role(install_environment, runtime):
    from moyai.deployment_checks import validate
    from moyai.install_config import prepare

    if runtime is not None:
        install_environment["MOYAI_MODAL_RUNTIME"] = runtime
    expected = runtime or "vm"
    values, _ = prepare(
        install_environment, image="moyai:test", topology="split", temporal="external",
        secrets_from_env=False,
    )
    assert values["MOYAI_MODAL_RUNTIME"] == expected
    for role in ("api", "coordinator", "broker", "worker"):
        configured = configuration(
            {**install_environment, **values}, None, role=role,
        )
        configured = configuration(configured, new_state(install_environment, configured), role=role)
        assert validate(configured).modal_runtime == expected


def test_existing_deployment_without_runtime_uses_vm(install_environment):
    from moyai.deployment_checks import validate

    _, values = prepared(install_environment)
    assert values.pop("MOYAI_MODAL_RUNTIME") == "vm"
    # The preflight's own fallback also applies when a caller passes old config.
    assert validate(values).modal_runtime == "vm"


@pytest.mark.parametrize("environment", [
    {"MOYAI_MODAL_RUNTIME": "vmm"},
    {"MOYAI_MODAL_RUNTIME": "vm", "MODAL_FUNCTION_RUNTIME": "gvisor"},
])
def test_invalid_or_conflicting_modal_runtime_is_rejected(install_environment, environment):
    with pytest.raises(InstallationError, match="RUNTIME"):
        configuration({**install_environment, **environment}, None)


def test_shared_configuration_uses_verified_schemas_and_persists_keys(install_environment):
    state, values = prepared(install_environment)
    write_state(install_environment, state)
    persisted = read_state(install_environment)
    assert persisted == state
    assert configuration(install_environment, persisted) == values
    for secret in ("private-password", "private-temporal", "private-inference", "private-modal-secret"):
        assert secret not in json.dumps(persisted)
    config = load_product_config(values)
    assert config is not None
    assert config.native_schema_mode == "verify"
    assert config.native_schema == "moyai_test_n"
    assert require_startable(config).run_worker


def test_split_configuration_reuses_all_authority_keys_but_selects_each_role(install_environment):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, _ = prepared(install_environment)
    configs = []
    for role in ("api", "coordinator", "broker", "worker"):
        values = configuration(install_environment, state, role=role)
        config = load_product_config(values)
        assert config is not None
        assert config.role == role
        require_startable(config)
        configs.append(config)
    assert {config.native_schema for config in configs} == {"moyai_test_n"}
    assert {config.native_task_queue for config in configs} == {"test-native-v1"}
    assert len({config.native_encryption_key.get_secret_value() for config in configs}) == 1
    assert all(config.native_encryption_key.get_secret_value() != state["secrets"]["ENCRYPTION_KEY"] for config in configs)


@pytest.mark.parametrize(("field", "changed"), [
    ("MOYAI_DATABASE_URL", "postgresql://user:password@other.example.test/moyai"),
    ("MOYAI_DATABASE_URL", "postgresql://user:password@localhost:5432/moyai?host=other.example.test"),
    ("OBJECT_STORAGE_BUCKET", "another-bucket"),
    ("MOYAI_INSTALL_TOPOLOGY", "split"),
    ("TEMPORAL_NAMESPACE", "another-namespace"),
    ("MOYAI_PUBLIC_URL", "https://another.example.test"),
    ("ENCRYPTION_KEY", "another-key"),
])
def test_restarts_reject_destination_and_key_changes(install_environment, field, changed):
    state, _ = prepared(install_environment)
    with pytest.raises(InstallationError):
        configuration({**install_environment, field: changed}, state)


def test_credential_rotation_preserves_stored_identity(install_environment):
    state, values = prepared(install_environment)
    rotated = configuration({
        **install_environment,
        "MOYAI_DATABASE_URL": "postgresql://user:rotated@localhost:5432/moyai",
        "LITELLM_API_KEY": "rotated",
        "TEMPORAL_API_KEY": "rotated",
        "MODAL_TOKEN_SECRET": "rotated",
    }, state)
    assert rotated["ENCRYPTION_KEY"] == values["ENCRYPTION_KEY"]
    assert rotated["MOYAI_INSTALL_SCOPE"] == values["MOYAI_INSTALL_SCOPE"]


def test_bundled_temporal_uses_internal_endpoint_and_separate_queue(install_environment):
    install_environment.update(
        MOYAI_TEMPORAL_MODE="bundled", TEMPORAL_ADDRESS="", TEMPORAL_NAMESPACE="",
    )
    _, values = prepared(install_environment)
    assert values["TEMPORAL_ADDRESS"] == "temporal:7233"
    assert values["TEMPORAL_NAMESPACE"] == "test-moyai"
    assert values["TEMPORAL_TLS"] == "false"
    assert values["TEMPORAL_API_KEY"] == ""


@pytest.mark.parametrize("origin", [
    "http://public.example.test", "https://name:password@example.test", "https://example.test/subpath",
])
def test_public_origin_validation_never_returns_input(install_environment, origin):
    with pytest.raises(InstallationError) as failure:
        configuration({**install_environment, "MOYAI_PUBLIC_URL": origin}, None)
    assert origin not in str(failure.value)


async def test_initialization_is_idempotent_and_does_not_repeat_schema_writes(install_environment, monkeypatch):
    monkeypatch.setattr(deployment.deployment_bootstrap, "require_empty", lambda values: None)
    checks = AsyncMock(return_value={"postgres": "connected"})
    provision = AsyncMock(return_value={"native": {"app_schema": "v13"}})
    verify = AsyncMock(return_value={"native": "verified"})
    monkeypatch.setattr(deployment.deployment_checks, "check", checks)
    monkeypatch.setattr(deployment.deployment_bootstrap, "provision", provision)
    monkeypatch.setattr(deployment.deployment_bootstrap, "verify", verify)
    first = await deployment.initialize(install_environment)
    original = read_state(install_environment)
    second = await deployment.initialize(install_environment)
    assert first["installation"] == "initialized"
    assert second["installation"] == "verified"
    assert read_state(install_environment) == original
    provision.assert_awaited_once()
    verify.assert_awaited_once()


async def test_failed_initialization_keeps_keys_and_requires_explicit_recovery(install_environment, monkeypatch):
    monkeypatch.setattr(deployment.deployment_bootstrap, "require_empty", lambda values: None)
    monkeypatch.setattr(deployment.deployment_checks, "check", AsyncMock(return_value={}))
    provision = AsyncMock(side_effect=InstallationError("Migration failed."))
    monkeypatch.setattr(deployment.deployment_bootstrap, "provision", provision)
    with pytest.raises(InstallationError, match="Migration failed"):
        await deployment.initialize(install_environment)
    state = read_state(install_environment)
    assert state is not None and state["phase"] == "failed"
    with pytest.raises(InstallationError, match="incomplete"):
        await deployment.initialize(install_environment)
    assert read_state(install_environment) == state
    provision.assert_awaited_once()


async def test_populated_schema_without_keys_is_never_adopted(install_environment, monkeypatch):
    def refuse(values):
        raise InstallationError("The target schema already contains data.")
    monkeypatch.setattr(deployment.deployment_bootstrap, "require_empty", refuse)
    with pytest.raises(InstallationError, match="already contains data"):
        await deployment.initialize(install_environment)
    assert read_state(install_environment) is None


async def test_external_namespace_command_does_not_create_cloud_resources(install_environment):
    with pytest.raises(InstallationError, match="restricted"):
        await deployment.dispatch(argparse.Namespace(command="namespace"), install_environment)


async def test_activation_refuses_existing_maintenance_intent(install_environment, monkeypatch):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, values = prepared(install_environment)
    monkeypatch.setattr(deployment, "health", AsyncMock())
    control = AsyncMock(return_value={
        "generation": 2, "request_key": "operator-maintenance", "state": "drained", "paused": True,
    })
    monkeypatch.setattr(deployment, "control", control)
    with pytest.raises(InstallationError, match="untouched"):
        await deployment.activate(install_environment, state, values)
    assert control.await_count == 1


def test_group_readable_secret_state_is_rejected(install_environment):
    state, _ = prepared(install_environment)
    write_state(install_environment, state)
    from moyai.deployment_config import state_path
    state_path(install_environment).chmod(0o640)
    with pytest.raises(InstallationError, match="0600"):
        read_state(install_environment)


def test_release_bundle_excludes_private_configuration_and_pins_image(tmp_path):
    from scripts.build_docker_bundle import FILES, build

    source = tmp_path / "source"
    shutil.copytree(Path(__file__).resolve().parents[2] / "deploy/docker", source)
    (source / ".env").write_text("LITELLM_API_KEY=do-not-package-this\n")
    (source / "installation.json").write_text('{"secrets": "do-not-package-this"}')
    image = "ghcr.io/berriai/moyai@sha256:" + "a" * 64
    output = tmp_path / "bundle.tar.gz"
    build(source, output, image, "b" * 40)
    with tarfile.open(output) as archive:
        assert set(archive.getnames()) == {
            "moyai-docker/" + name for name in (*FILES, "image.txt", "release.json")
        }
        for entry in archive:
            content = archive.extractfile(entry).read()
            assert b"do-not-package-this" not in content
        assert image.encode() in archive.extractfile("moyai-docker/.env.example").read()


async def test_upgrade_refuses_unknown_or_active_source_before_stopping_anything(install_environment, monkeypatch):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, values = prepared(install_environment)
    state["phase"] = "initialized"
    monkeypatch.setattr(deployment, "health", AsyncMock())
    control = AsyncMock(return_value={"paused": True})
    monkeypatch.setattr(deployment, "control", control)
    with pytest.raises(InstallationError, match="existing maintenance"):
        await deployment.begin_upgrade(
            argparse.Namespace(revision="2" * 40, timeout=1), install_environment, state, values,
        )
    assert "upgrade" not in state
    assert control.await_count == 1


async def test_upgrade_requires_old_worker_retirement_before_schema_writes(install_environment, monkeypatch):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, _ = prepared(install_environment)
    state.update(phase="upgrading", upgrade={"phase": "drained", "generation": 4, "target": "2" * 40})
    values = configuration({**install_environment, "MOYAI_BUILD_SHA": "2" * 40}, state, role="worker")
    monkeypatch.setattr(deployment, "control", AsyncMock(return_value={
        "state": "drained", "generation": 4, "workers": [{"pid": 42}],
    }))
    provision = AsyncMock()
    monkeypatch.setattr(deployment.deployment_bootstrap, "provision", provision)
    with pytest.raises(InstallationError, match="worker must be stopped"):
        await deployment.apply_upgrade(install_environment, state, values)
    provision.assert_not_awaited()


async def test_unknown_migration_outcome_is_not_automatically_retried(install_environment, monkeypatch):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, _ = prepared(install_environment)
    state.update(phase="upgrading", upgrade={"phase": "drained", "generation": 4, "target": "2" * 40})
    values = configuration({**install_environment, "MOYAI_BUILD_SHA": "2" * 40}, state, role="worker")
    monkeypatch.setattr(deployment, "control", AsyncMock(return_value={
        "state": "drained", "generation": 4, "workers": [],
    }))
    provision = AsyncMock(side_effect=InstallationError("Unknown migration outcome"))
    monkeypatch.setattr(deployment.deployment_bootstrap, "provision", provision)
    with pytest.raises(InstallationError, match="Unknown migration"):
        await deployment.apply_upgrade(install_environment, state, values)
    persisted = read_state(install_environment)
    assert persisted["upgrade"]["phase"] == "migrating"
    with pytest.raises(InstallationError, match="recorded target"):
        await deployment.apply_upgrade(install_environment, persisted, values)
    provision.assert_awaited_once()


def test_standalone_runtime_lock_excludes_second_instance_and_migration(install_environment):
    with (
        standalone_lock(install_environment),
        pytest.raises(InstallationError, match="still running"),
        standalone_lock(install_environment),
    ):
        pytest.fail("Another process acquired the runtime lock.")
    with standalone_lock(install_environment):
        pass


async def test_standalone_upgrade_refuses_unfinished_work(install_environment, monkeypatch):
    state, _ = prepared(install_environment)
    state["phase"] = "initialized"
    values = configuration({**install_environment, "MOYAI_BUILD_SHA": "2" * 40}, state)
    monkeypatch.setattr(deployment, "standalone_unsettled", AsyncMock(return_value={
        "turns": 1, "effects": 0, "requests": 0,
    }))
    provision = AsyncMock()
    monkeypatch.setattr(deployment.deployment_bootstrap, "provision", provision)
    with pytest.raises(InstallationError, match="Unfinished standalone"):
        await deployment.standalone_upgrade(
            argparse.Namespace(source_revision="1" * 40), install_environment, state, values,
        )
    assert state["phase"] == "initialized"
    provision.assert_not_awaited()


async def test_standalone_offline_upgrade_retains_keys_and_records_revision(install_environment, monkeypatch):
    state, _ = prepared(install_environment)
    state["phase"] = "initialized"
    original_keys = dict(state["secrets"])
    values = configuration({**install_environment, "MOYAI_BUILD_SHA": "2" * 40}, state)
    monkeypatch.setattr(deployment, "standalone_unsettled", AsyncMock(return_value={
        "turns": 0, "effects": 0, "requests": 0,
    }))
    monkeypatch.setattr(deployment.deployment_bootstrap, "provision", AsyncMock(return_value={"native": "v13"}))
    result = await deployment.standalone_upgrade(
        argparse.Namespace(source_revision="1" * 40), install_environment, state, values,
    )
    assert result["revision"] == "2" * 40
    assert state["secrets"] == original_keys
    assert state["phase"] == "initialized"
    assert state["last_upgrade"]["phase"] == "completed"
    assert state["last_upgrade"]["source"] == "1" * 40
    assert "upgrade" not in state


@pytest.mark.parametrize("fault", [None, "unavailable", "redirect", "build", "owner", "role", "core"])
async def test_health_requires_the_public_route_to_reach_the_verified_broker(
    install_environment, monkeypatch, fault,
):
    install_environment.update(
        MOYAI_INSTALL_TOPOLOGY="split",
        CLOUDFLARE_ACCESS_CLIENT_ID="synthetic-access-id",
        CLOUDFLARE_ACCESS_CLIENT_SECRET="synthetic-access-secret",
    )
    _, values = prepared(install_environment)
    public_calls = []

    def respond(request):
        public = request.url.host == "workspace.example.test"
        role = "broker" if public else request.url.host
        headers = {
            "X-Moyai-Core": "pydantic-v1", "X-Moyai-Role": role,
            "X-Moyai-Build": values["MOYAI_BUILD_SHA"], "X-Moyai-Owner": role + "-owner",
        }
        if public:
            public_calls.append(request)
            assert request.url.path == "/broker/health"
            assert request.headers["CF-Access-Client-Id"] == "synthetic-access-id"
            assert request.headers["CF-Access-Client-Secret"] == "synthetic-access-secret"
            if fault in {"unavailable", "redirect"}:
                return httpx.Response(502 if fault == "unavailable" else 302)
            if fault:
                headers["X-Moyai-" + fault.title()] = "different"
        else:
            assert "CF-Access-Client-Secret" not in request.headers
        return httpx.Response(200, headers=headers)

    client = httpx.AsyncClient
    monkeypatch.setattr(deployment.httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    if fault:
        with pytest.raises(InstallationError, match="public broker"):
            await deployment.health(values)
    else:
        await deployment.health(values)
    assert len(public_calls) == 1


@pytest.mark.parametrize("command", ["activate", "resume"])
async def test_broker_unavailable_never_resumes_or_opens_ingress(install_environment, monkeypatch, command):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, _ = prepared(install_environment)
    state["phase"] = "initialized"
    if command == "resume":
        state["upgrade"] = {"phase": "awaiting_resume", "generation": 4}
    write_state(install_environment, state)
    deployment.ingress(install_environment, ready=True)
    monkeypatch.setattr(deployment, "health", AsyncMock(side_effect=InstallationError("public broker unavailable")))
    control = AsyncMock()
    monkeypatch.setattr(deployment, "control", control)
    with pytest.raises(InstallationError, match="public broker"):
        await deployment.dispatch(argparse.Namespace(command=command), install_environment)
    control.assert_not_awaited()
    assert read_state(install_environment) == state
    assert not (Path(install_environment["DATA_DIR"]) / "ingress/ready").exists()


@pytest.mark.parametrize("command", ["activate", "resume"])
async def test_ingress_opens_only_after_confirmed_resume(install_environment, monkeypatch, command):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, _ = prepared(install_environment)
    state["phase"] = "initialized"
    if command == "resume":
        state.update(activation_pending=False, upgrade={"phase": "awaiting_resume", "generation": 0})
    write_state(install_environment, state)
    marker = Path(install_environment["DATA_DIR"]) / "ingress/ready"
    events = []

    async def health(values):
        assert not marker.exists()
        events.append("broker-ready")

    async def control(values, operation, **kwargs):
        assert not marker.exists()
        events.append(operation)
        worker = {"pid": 42, "backend_start": "2026-10-16T00:00:00+00:00",
                  "source_policy": "a" * 64, "worker_policy": "b" * 64}
        status = {
            "generation": 0, "request_key": "bootstrap", "state": "drained" if operation == "status" else "running",
            "paused": operation == "status", "counts": {},
            "protocol": 1, "core": "pydantic-v1", "native_schema": values["MOYAI_NATIVE_SCHEMA"],
            "task_queue": values["MOYAI_NATIVE_TASK_QUEUE"], "workers": [{**worker, "maintenance_drain": False}],
            "source_policy": worker["source_policy"],
            "requester": {"pid": worker["pid"], "backend_start": worker["backend_start"]},
        }
        if operation == "resume":
            intent = kwargs.get("intent")
            if intent:
                status.update(generation=intent["expected_generation"] + 1, request_key=intent["request_key"])
        return status

    monkeypatch.setattr(deployment, "health", health)
    monkeypatch.setattr(deployment, "control", control)
    await deployment.dispatch(argparse.Namespace(command=command), install_environment)
    assert events == ["broker-ready", "status", "status", "resume"]
    assert marker.is_file() and marker.stat().st_mode & 0o777 == 0o644
    persisted = read_state(install_environment)
    assert persisted["phase"] == "initialized"
    assert not persisted["activation_pending"]
    assert "upgrade" not in persisted


async def test_split_provision_uses_installed_interpreter_for_canonical_schema(install_environment, monkeypatch):
    import subprocess
    import sys

    from moyai import deployment_bootstrap, product_start

    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    _, values = prepared(install_environment)
    calls = []

    def migrate(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, '{"schema_revision":21,"status":"ready"}', '')

    monkeypatch.setattr(deployment_bootstrap.subprocess, "run", migrate)
    monkeypatch.setattr(product_start, "prepare_native_schema", AsyncMock(return_value={"app_schema": "verified"}))
    result = await deployment_bootstrap.provision(values, fresh=False)
    assert calls[0][0] == [sys.executable, "-m", "app.schema_migrations", "--apply"]
    assert calls[0][1]["MOYAI_DATABASE_INITIALIZE"] == "false"
    assert result == {"product": {"schema_revision": 21, "status": "ready"}, "native": {"app_schema": "verified"}}
