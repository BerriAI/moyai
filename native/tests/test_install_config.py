from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from test_deployment_package import install_environment as install_fixture
from test_deployment_package import prepared
from test_deployment_recovery import NativeControl

from moyai import deployment, install_config
from moyai.deployment_config import InstallationError, read_state, write_state

install_environment = install_fixture
IMAGE = "ghcr.io/berriai/moyai@sha256:" + "a" * 64


def test_installer_retains_explicit_computer_lifetime_and_idle_window(install_environment):
    values, _ = install_config.prepare(
        {**install_environment, "MOYAI_COMPUTER_TIMEOUT": "28800", "MOYAI_COMPUTER_IDLE_TIMEOUT": "60"},
        image=IMAGE, topology="split", temporal="external", secrets_from_env=False,
    )
    assert values["MOYAI_COMPUTER_TIMEOUT"] == "28800"
    assert values["MOYAI_COMPUTER_IDLE_TIMEOUT"] == "60"


@pytest.mark.parametrize("external_secrets", [False, True])
def test_slack_configuration_survives_installation_without_leaking_external_secrets(
    install_environment, external_secrets, tmp_path,
):
    slack = {
        "SLACK_CLIENT_ID": "123.456", "SLACK_CLIENT_SECRET": "client-secret",
        "SLACK_SIGNING_SECRET": "signing-secret", "SLACK_BOT_ENABLED": "true",
        "SLACK_SESSION_USERS": "U123", "SLACK_DM_ENABLED": "false",
        "SLACK_THREAD_CHAT_ENABLED": "false", "SLACK_IDENTITY_LINKING_ENABLED": "false",
        "SLACK_BOT_TOKEN": "test-bot-token", "SLACK_TEAM_ID": "T123",
        "GOOGLE_ALLOWED_DOMAINS": "example.com",
    }
    values, overlay = install_config.prepare(
        {**install_environment, **slack}, image=IMAGE, topology="split", temporal="external",
        secrets_from_env=external_secrets,
    )
    install_config.write_configuration(tmp_path, values, overlay)
    content = "".join(path.read_text() for path in tmp_path.iterdir())
    for name, value in slack.items():
        if external_secrets and name in install_config.SECRETS:
            assert value not in content
            assert all(name in service["environment"] for service in overlay["services"].values())
        else:
            assert values[name] == value


@pytest.mark.parametrize("provider", ["modal", "lambda"])
def test_interactive_installer_requests_only_selected_sandbox_credentials(
    install_environment, tmp_path, monkeypatch, provider,
):
    environment = {**install_environment, "SANDBOX_PROVIDER": provider}
    if provider == "lambda":
        environment.pop("MODAL_TOKEN_ID", None)
        environment.pop("MODAL_TOKEN_SECRET", None)
    answers = {
        "Sandbox provider (modal/lambda)": provider,
        "Lambda MicroVM image ARN": "arn:aws:lambda:us-east-1:123456789012:microvm-image:test",
        "Pinned Lambda MicroVM image version": "1.0",
        "Private S3 checkpoint bucket": "test-checkpoints",
    }
    labels = []

    def answer(label, default, **kwargs):
        labels.append(label)
        return answers.get(label, default)

    monkeypatch.setattr(install_config, "prompt", answer)
    monkeypatch.setattr(install_config.os, "environ", environment)
    monkeypatch.setattr(sys, "argv", [
        "moyai.install_config", "--image", IMAGE, "--directory", str(tmp_path),
        "--topology", "split", "--temporal", "external",
    ])
    install_config.main()
    assert ("Modal token ID" in labels) is (provider == "modal")
    assert ("Lambda MicroVM image ARN" in labels) is (provider == "lambda")
    assert f"SANDBOX_PROVIDER={provider}\n" in (tmp_path / ".env").read_text()


@pytest.mark.parametrize("topology", ["standalone", "split"])
@pytest.mark.parametrize("temporal", ["external", "bundled"])
@pytest.mark.parametrize("external_secrets", [False, True])
def test_configuration_selects_matching_services_and_temporal(
    install_environment, topology, temporal, external_secrets, tmp_path,
):
    install_environment["MOYAI_TEMPORAL_DATABASE_PASSWORD"] = "test-temporal-password"
    values, overlay = install_config.prepare(
        install_environment, image=IMAGE, topology=topology, temporal=temporal, secrets_from_env=external_secrets,
    )
    expected = ["compose.yaml" if topology == "standalone" else "compose.split.yaml"]
    if temporal == "bundled":
        expected.append("compose.temporal.yaml")
    if external_secrets:
        expected.append(install_config.ENVIRONMENT_FILE)
    assert values["COMPOSE_FILE"].split(":") == expected
    assert values["MOYAI_INSTALL_TOPOLOGY"] == topology
    assert values["MOYAI_TEMPORAL_MODE"] == temporal
    assert values["MOYAI_IMAGE"] == IMAGE
    install_config.write_configuration(tmp_path, values, overlay)
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert read_state(install_environment) is None  # Configuration is not database initialization.
    if external_secrets:
        contents = "".join(path.read_text() for path in tmp_path.iterdir())
        for key in install_config.SECRETS:
            secret = install_environment.get(key)
            if secret:
                assert secret not in contents
        services = {"setup", "app"} if topology == "standalone" else {
            "setup", "coordinator", "broker", "worker", "api", "activate",
        }
        if temporal == "bundled":
            services.add("temporal-namespace")
        assert set(overlay["services"]) == services
        assert all(all(value is None for value in item["environment"].values()) for item in overlay["services"].values())
    else:
        assert overlay is None
        assert values["LITELLM_API_KEY"] == install_environment["LITELLM_API_KEY"]


def test_configuration_preserves_literal_credentials_and_existing_files(install_environment, tmp_path):
    literal = "\"literal$NOT_AN_ENV_VAR#'\\value"
    install_environment["LITELLM_API_KEY"] = literal
    values, overlay = install_config.prepare(
        install_environment, image=IMAGE, topology="standalone", temporal="external", secrets_from_env=False,
    )
    install_config.write_configuration(tmp_path, values, overlay)
    original = (tmp_path / ".env").read_bytes()
    assert f"LITELLM_API_KEY={literal}\n".encode() in original
    with pytest.raises(InstallationError, match="already exists"):
        install_config.write_configuration(tmp_path, {**values, "LITELLM_API_KEY": "replacement"}, None)
    assert (tmp_path / ".env").read_bytes() == original


def test_configuration_rejects_newline_injection_without_printing_secret(install_environment):
    secret = "private-value\nMOYAI_IMAGE=attacker"
    with pytest.raises(InstallationError) as error:
        install_config.prepare(
            {**install_environment, "LITELLM_API_KEY": secret}, image=IMAGE,
            topology="standalone", temporal="external", secrets_from_env=False,
        )
    assert "LITELLM_API_KEY" in str(error.value) and "private-value" not in str(error.value)


@pytest.mark.parametrize("secrets_from_env", [False, True])
def test_bundled_password_is_generated_and_retained_without_an_extra_input(
    install_environment, tmp_path, secrets_from_env,
):
    values, _ = install_config.prepare(
        install_environment, image=IMAGE, topology="standalone", temporal="bundled", secrets_from_env=secrets_from_env,
    )
    assert len(values["MOYAI_TEMPORAL_DATABASE_PASSWORD"]) == 48
    assert values.get("TEMPORAL_API_KEY", "") == "" and values["TEMPORAL_TLS"] == "false"
    install_config.write_configuration(tmp_path, values, None)
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert values["MOYAI_TEMPORAL_DATABASE_PASSWORD"] in (tmp_path / ".env").read_text()


def test_unattended_configuration_defaults_to_the_supplied_external_temporal(
    install_environment, tmp_path, monkeypatch,
):
    monkeypatch.setattr(install_config.os, "environ", install_environment)
    monkeypatch.setattr(sys, "argv", [
        "moyai.install_config", "--image", IMAGE, "--directory", str(tmp_path),
        "--from-env", "--secrets-from-env",
    ])
    install_config.main()
    content = (tmp_path / ".env").read_text()
    assert "MOYAI_TEMPORAL_MODE=external\n" in content
    assert f"TEMPORAL_ADDRESS={install_environment['TEMPORAL_ADDRESS']}\n" in content
    assert "compose.temporal.yaml" not in content


def test_configuration_never_follows_an_existing_env_symlink(tmp_path):
    destination = tmp_path / "other"
    (tmp_path / ".env").symlink_to(destination)
    with pytest.raises(InstallationError, match="already exists"):
        install_config.write_configuration(tmp_path, {"MOYAI_IMAGE": IMAGE}, None)
    assert not destination.exists()


def test_configuration_publication_does_not_replace_a_concurrent_file(tmp_path, monkeypatch):
    link = install_config.os.link

    def race(source, target):
        if Path(target).name == ".env":
            Path(target).write_text("owned-by-another-operator\n")
        link(source, target)

    monkeypatch.setattr(install_config.os, "link", race)
    with pytest.raises(FileExistsError):
        install_config.write_configuration(tmp_path, {"MOYAI_IMAGE": IMAGE}, {"services": {}})
    assert (tmp_path / ".env").read_text() == "owned-by-another-operator\n"
    assert not (tmp_path / install_config.ENVIRONMENT_FILE).exists()
    assert not list(tmp_path.glob(".moyai-config-*"))


@pytest.mark.parametrize("interrupt_at", [".env", install_config.ENVIRONMENT_FILE])
def test_interrupt_after_publication_keeps_a_complete_configuration(tmp_path, monkeypatch, interrupt_at):
    link = install_config.os.link

    def interrupt(source, target):
        link(source, target)
        if Path(target).name == interrupt_at:
            raise KeyboardInterrupt

    monkeypatch.setattr(install_config.os, "link", interrupt)
    with pytest.raises(KeyboardInterrupt):
        install_config.write_configuration(tmp_path, {"MOYAI_IMAGE": IMAGE}, {"services": {}})
    committed = interrupt_at == ".env"
    assert (tmp_path / ".env").exists() is committed
    assert (tmp_path / install_config.ENVIRONMENT_FILE).exists() is committed
    assert not list(tmp_path.glob(".moyai-config-*"))


async def test_preflight_is_offline_and_does_not_create_installation_state(install_environment, monkeypatch):
    check = AsyncMock()
    provision = AsyncMock()
    monkeypatch.setattr(deployment.deployment_checks, "check", check)
    monkeypatch.setattr(deployment.deployment_bootstrap, "provision", provision)
    result = await deployment.dispatch(argparse.Namespace(command="validate"), install_environment)
    assert result == {"configuration": "valid", "service_connections": "not_checked"}
    assert read_state(install_environment) is None
    check.assert_not_awaited()
    provision.assert_not_awaited()


@pytest.mark.parametrize("change", ["revision", "maintenance", "destination"])
def test_preflight_rejects_unplanned_changes_to_an_existing_installation(install_environment, change):
    state, _ = prepared(install_environment)
    state.update(phase="initialized", activation_pending=False)
    write_state(install_environment, state)
    if change == "revision":
        install_environment["MOYAI_BUILD_SHA"] = "2" * 40
    elif change == "maintenance":
        state["upgrade"] = {"phase": "resuming"}
        write_state(install_environment, state)
    else:
        install_environment["OBJECT_STORAGE_BUCKET"] = "different-bucket"
    with pytest.raises(InstallationError):
        deployment.validate_configuration(install_environment)
    assert read_state(install_environment) == state


@pytest.mark.parametrize("blocked", ["activation", "upgrade", "paused", "ingress"])
async def test_readiness_never_reports_a_gated_installation_as_serving(install_environment, monkeypatch, blocked):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, values = prepared(install_environment)
    state.update(phase="initialized", activation_pending=blocked == "activation")
    if blocked == "upgrade":
        state["upgrade"] = {"phase": "resuming"}
    if blocked != "ingress":
        deployment.ingress(install_environment, ready=True)
    monkeypatch.setattr(deployment, "health", AsyncMock())
    monkeypatch.setattr(deployment, "control", AsyncMock(return_value={"paused": blocked == "paused"}))
    with pytest.raises(InstallationError):
        await deployment.ready(install_environment, state, values)


async def test_standalone_readiness_reports_http_health_separately_from_model_execution(
    install_environment, monkeypatch,
):
    state, values = prepared(install_environment)
    state.update(phase="initialized", activation_pending=False)
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "status": "ok", "engine": "pydantic-ai", "durability": "temporal",
    }))
    monkeypatch.setattr(deployment.httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=transport))
    report = await deployment.ready(install_environment, state, values)
    assert report["http"] == "healthy" and report["model_execution"] == "not_checked"
    assert not any(secret in json.dumps(report) for secret in state["secrets"].values())


@pytest.mark.parametrize("failure", ["before", "after", "paused"])
async def test_initial_activation_recovers_only_its_original_resume(install_environment, monkeypatch, failure):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, values = prepared(install_environment)
    state["phase"] = "initialized"
    write_state(install_environment, state)
    native = NativeControl(values, paused=True, failure=failure)
    native.status.update(generation=0, request_key="bootstrap")
    monkeypatch.setattr(deployment, "control", native)
    monkeypatch.setattr(deployment, "health", AsyncMock())
    args = argparse.Namespace(command="activate")
    with pytest.raises(InstallationError if failure == "paused" else OSError):
        await deployment.dispatch(args, install_environment)
    pending = read_state(install_environment)
    assert pending["activation_pending"]
    original = pending["activation_command"]
    assert not (Path(install_environment["DATA_DIR"]) / "ingress/ready").exists()
    await deployment.dispatch(args, install_environment)
    assert not read_state(install_environment)["activation_pending"]
    assert native.status["generation"] == 1
    assert all(intent == original for _, intent in native.calls)
    assert len(native.calls) == (1 if failure == "after" else 2)
    assert (Path(install_environment["DATA_DIR"]) / "ingress/ready").exists()


async def test_uncommitted_activation_does_not_adopt_queued_work(install_environment, monkeypatch):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, values = prepared(install_environment)
    state["phase"] = "initialized"
    write_state(install_environment, state)
    native = NativeControl(values, paused=True, failure="before")
    native.status.update(generation=0, request_key="bootstrap")
    monkeypatch.setattr(deployment, "control", native)
    monkeypatch.setattr(deployment, "health", AsyncMock())
    args = argparse.Namespace(command="activate")
    with pytest.raises(OSError):
        await deployment.dispatch(args, install_environment)
    native.status["counts"] = {"queued": 1}
    with pytest.raises(InstallationError, match="cannot adopt"):
        await deployment.dispatch(args, install_environment)
    assert len(native.calls) == 1
    assert read_state(install_environment)["activation_pending"]


@pytest.mark.parametrize("command", ["validate", "ready", "status"])
def test_read_only_commands_do_not_wait_for_the_maintenance_file_lock(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["moyai.deployment", command])
    monkeypatch.setattr(deployment, "dispatch", AsyncMock(return_value={"read_only": True}))
    monkeypatch.setattr(deployment.fcntl, "flock", lambda *args: pytest.fail("Read-only command tried to take the lock."))
    deployment.main()
    assert json.loads(capsys.readouterr().out) == {"read_only": True}
    assert not (tmp_path / ".installation.lock").exists()


def test_installer_preserves_explicit_broker_url(install_environment):
    install_environment['MOYAI_BROKER_URL'] = 'https://broker.example.test'
    values, _ = install_config.prepare(install_environment, image=IMAGE, topology='split', temporal='external', secrets_from_env=False)
    assert values['MOYAI_BROKER_URL'] == 'https://broker.example.test'
