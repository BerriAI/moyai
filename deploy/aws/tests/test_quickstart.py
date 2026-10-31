"""Installer boundaries; real AWS/Slack acceptance is documented separately."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

HERE = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bootstrap = module("bootstrap")
quickstart = module("quickstart")


def secret():
    return {key: "x" * 32 for key in ("WORKSPACE_PASSWORD", "database_password", "gateway_key")}


def config(bundle):
    return {
        "url": "https://moyai.example.com", "region": "us-east-1", "database_host": "db.example.com",
        "bucket": "moyai-test", "secret_arn": "runtime-secret", "image_arn": "guest-image", "image_version": "1.0",
        "moyai_image": "app@sha256:" + "a" * 64, "litellm_image": "gateway@sha256:" + "b" * 64,
        "bedrock_model_id": "model", "computer_lifetime": 28800, "computer_idle_timeout": 600,
        "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
    }


@pytest.mark.parametrize("value", [[], {"AWS_SECRET_ACCESS_KEY": "not-allowed"}, {"WORKSPACE_PASSWORD": "short"}, {"SLACK_CLIENT_ID": "only-one-field"}])
def test_rejects_incomplete_or_unexpected_secrets(value):
    with pytest.raises((ValueError, TypeError)):
        bootstrap.validate_secret(value)


def test_database_password_is_encoded_and_tls_verifies_hostname():
    url = bootstrap.database_url("db.example", "moyai", "p@ss:/?#%", "moyai", "/run/rds-ca.pem")
    assert "p%40ss%3A%2F%3F%23%25@db.example" in url
    assert "sslmode=verify-full" in url


def test_fresh_install_passes_actual_application_configuration_validation(tmp_path, monkeypatch):
    from moyai.install_config import prepare, write_configuration

    monkeypatch.setattr(bootstrap, "APP", tmp_path)
    monkeypatch.setattr(bootstrap, "CONTROL", tmp_path)
    (tmp_path / "host-ready").touch()
    content = quickstart.application_bundle()
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(content)
    values = {**config(content), "image_arn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:workspace"}
    monkeypatch.setattr(bootstrap, "aws", Mock(return_value={"SecretString": json.dumps(secret())}))
    monkeypatch.setattr(bootstrap, "urlopen", lambda *args, **kwargs: io.BytesIO(b"public CA"))
    for name in ("pull_application", "configure_database", "configure_gateway"):
        monkeypatch.setattr(bootstrap, name, Mock())

    def run(command, **kwargs):
        if command[:3] == ["sh", "moyai.sh", "configure"]:
            environment = {**kwargs["env"], "MOYAI_BUILD_SHA": "a" * 40}
            configured, overlay = prepare(environment, image=values["moyai_image"], topology="split", temporal="bundled", secrets_from_env=False)
            write_configuration(tmp_path, configured, overlay)
        return ""

    monkeypatch.setattr(bootstrap, "run", run)
    bootstrap.install(values, bundle)
    assert (tmp_path / "aws-installation.json").is_file()


def test_bundle_checksum_precedes_extraction_or_secret_access(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "CONTROL", tmp_path)
    (tmp_path / "host-ready").touch()
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"corrupt")
    fetch = Mock()
    monkeypatch.setattr(bootstrap, "aws", fetch)
    with pytest.raises(ValueError, match="checksum"):
        bootstrap.install(config(b"original"), bundle)
    fetch.assert_not_called()


def test_resume_preserves_published_secrets_and_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "CONTROL", tmp_path)
    monkeypatch.setattr(bootstrap, "APP", tmp_path)
    (tmp_path / "host-ready").touch()
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"not extracted during resume")
    identity = config(bundle.read_bytes())
    (tmp_path / "aws-installation-pending.json").write_text(json.dumps(identity))
    original = "COMPOSE_FILE=compose.split.yaml:compose.temporal.yaml\nWORKSPACE_PASSWORD=keep-this-value\n"
    (tmp_path / ".env").write_text(original)
    calls = Mock(return_value="")
    monkeypatch.setattr(bootstrap, "run", calls)
    monkeypatch.setattr(bootstrap, "aws", Mock(side_effect=AssertionError("Must not fetch or rotate secrets")))
    bootstrap.install(identity, bundle)
    contents = (tmp_path / ".env").read_text()
    assert "WORKSPACE_PASSWORD=keep-this-value\n" in contents
    assert contents.count(":compose.aws.json") == 1
    assert json.loads((tmp_path / "aws-installation.json").read_text()) == identity
    bootstrap.install(identity, bundle)
    assert (tmp_path / ".env").read_text() == contents
    assert [call.args[0][2] for call in calls.call_args_list] == ["up", "check", "check"]


def test_existing_installation_refuses_changed_destinations(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "CONTROL", tmp_path)
    monkeypatch.setattr(bootstrap, "APP", tmp_path)
    (tmp_path / "host-ready").touch()
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"bundle")
    identity = config(bundle.read_bytes())
    (tmp_path / "aws-installation.json").write_text(json.dumps(identity))
    with pytest.raises(RuntimeError, match="already exists"):
        bootstrap.install({**identity, "bucket": "different"}, bundle)


def test_existing_runtime_credentials_are_not_overwritten(tmp_path):
    requested = tmp_path / "secret.json"
    requested.write_text(json.dumps({"WORKSPACE_PASSWORD": "different"}))
    manager = Mock()
    manager.get_secret_value.return_value = {"SecretString": json.dumps(secret())}
    session = Mock()
    session.client.return_value = manager
    with pytest.raises(ValueError, match="not overwritten"):
        quickstart.initialize_secret(session, {"secret_arn": "test"}, requested)
    manager.put_secret_value.assert_not_called()


def test_bundle_and_guest_are_reproducible(tmp_path):
    assert quickstart.application_bundle() == quickstart.application_bundle()
    assert quickstart.guest_archive(tmp_path / "a.zip") == quickstart.guest_archive(tmp_path / "b.zip")


def test_ssm_command_carries_identifiers_not_credentials():
    session = Mock()
    ssm = Mock()
    s3 = Mock()
    session.client.side_effect = lambda name: {"ssm": ssm, "s3": s3}[name]
    ssm.send_command.return_value = {"Command": {"CommandId": "test"}}
    settings = {**config(b"bundle"), "instance_id": "i-test", "database_secret_arn": "rds-secret"}
    quickstart.start_install(session, settings, {"image_arn": "guest", "image_version": "1.0"})
    command = ssm.send_command.call_args.kwargs["Parameters"]["commands"][0]
    assert "get-secret-value" not in command
    assert "WORKSPACE_PASSWORD" not in command
    assert "flock -n 9" in command
    assert "sha256sum -c" in command
    assert ssm.send_command.call_args.kwargs["InstanceIds"] == ["i-test"]
