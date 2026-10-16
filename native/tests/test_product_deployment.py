from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from moyai.product_config import (
    NATIVE_KEY_FORMAT,
    NATIVE_OBJECT_FORMAT,
    ProductConfigurationError,
    load_product_config,
    select_core,
)
from moyai.product_deployment import (
    ProductDeploymentError,
    build_role_plan,
    candidate_role_fingerprint,
    check_runtime_dependencies,
    require_startable,
    verify_product_schema,
)
from moyai.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def environment(tmp_path):
    return {
        "MOYAI_CORE": "pydantic", "MOYAI_PRODUCT_MODE": "existing", "MOYAI_RUNTIME_ROLE": "api",
        "MOYAI_DATABASE_URL": "postgresql://synthetic:password@127.0.0.1/isolated",
        "MOYAI_DATABASE_SCHEMA": "moyai", "MOYAI_NATIVE_SCHEMA": "moyai_native_test",
        "MOYAI_SCHEMA_MODE": "verify", "MOYAI_DATABASE_INITIALIZE": "false",
        "TEMPORAL_ENABLED": "true", "MOYAI_SEPARATE_BROKER": "true", "MOYAI_COORDINATOR_HANDOFF": "true",
        "SESSION_SECRET": "synthetic-original-session-secret",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "MOYAI_NATIVE_ENCRYPTION_KEY": "synthetic-native-key-with-at-least-32-characters",
        "MOYAI_NATIVE_KEY_FORMAT": NATIVE_KEY_FORMAT, "MOYAI_NATIVE_OBJECT_FORMAT": NATIVE_OBJECT_FORMAT,
        "MOYAI_NATIVE_OBJECT_PREFIX": "pydantic-v1", "OBJECT_STORAGE_PREFIX": "moyai",
        "OBJECT_STORAGE_BUCKET": "synthetic-bucket", "OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:9000",
        "OBJECT_STORAGE_ACCESS_KEY_ID": "synthetic-key-id", "OBJECT_STORAGE_SECRET_ACCESS_KEY": "synthetic-secret",
        "OBJECT_STORAGE_SESSION_TOKEN": "synthetic-session",
        "TEMPORAL_ADDRESS": "127.0.0.1:7233", "TEMPORAL_NAMESPACE": "isolated",
        "TEMPORAL_TASK_QUEUE": "existing-sessions-v1", "MOYAI_NATIVE_TASK_QUEUE": "isolated-pydantic-v1",
        "MOYAI_PUBLIC_URL": "http://127.0.0.1:8796",
        "LITELLM_API_BASE": "http://127.0.0.1:8000/v1", "LITELLM_API_KEY": "synthetic-gateway",
        "AGENT_MODEL": "openai/gpt-6-astra",
        "DATA_DIR": str(tmp_path / "product"),
    }


def configured(env):
    config = load_product_config(env)
    assert config is not None
    return config


def test_legacy_default_and_standalone_remain_usable_without_product_sources(tmp_path):
    assert select_core({}) == "legacy"
    assert load_product_config({"MOYAI_DATABASE_URL": "invalid"}) is None
    standalone = configured({"MOYAI_CORE": "pydantic"})
    assert standalone.product_mode == "standalone"
    assert standalone.native_schema_mode == "bootstrap"
    assert standalone.native_overrides()["database_schema"] == ""
    assert require_startable(standalone).run_dispatcher
    with pytest.raises(ProductConfigurationError, match="no existing product"):
        standalone.legacy_settings()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("overrides", [
    {"MOYAI_DATABASE_URL": "postgresql://127.0.0.1/existing"},
    {"MOYAI_PRODUCT_MODE": "standalone", "MOYAI_DATABASE_SCHEMA": "moyai"},
    {"MOYAI_PRODUCT_MODE": "standalone", "MOYAI_SCHEMA_MODE": "verify"},
    {"RENDER_EXTERNAL_URL": "https://synthetic.example.test"},
])
def test_native_selection_cannot_silently_bootstrap_an_existing_product(overrides):
    with pytest.raises(ProductConfigurationError):
        configured({"MOYAI_CORE": "pydantic", **overrides})


def test_snapshot_uses_real_product_aliases_without_ambient_environment_or_dotenv(
    environment, monkeypatch, tmp_path,
):
    from app.config import Settings as ProductSettings

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SESSION_SECRET=dotenv-secret\nOBJECT_STORAGE_PREFIX=dotenv-prefix\n")
    monkeypatch.setenv("SESSION_SECRET", "ambient-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", "ambient-invalid")
    monkeypatch.setenv("TEMPORAL_TLS", "false")
    environment["PUBLIC_URL"] = "https://lower-priority.invalid"
    environment["GOOGLE_ALLOWED_DOMAINS"] = "example.test"
    config = configured(environment)
    source = config.legacy_settings()
    assert isinstance(source, ProductSettings)
    assert source.session_secret == environment["SESSION_SECRET"]
    assert source.encryption_key == environment["ENCRYPTION_KEY"]
    assert source.google_allowed_domains == "example.test"
    assert source.public_url == environment["MOYAI_PUBLIC_URL"]
    assert source.temporal_tls is True  # Product default, not prototype default.
    assert source.object_storage_prefix == "moyai"
    source.session_secret = "mutated-copy"
    assert config.legacy_settings().session_secret == environment["SESSION_SECRET"]
    native = Settings(_env_file=None, **config.native_overrides())
    assert native.encryption_key.get_secret_value() == environment["MOYAI_NATIVE_ENCRYPTION_KEY"]
    assert native.data_dir == Path(environment["DATA_DIR"]) / "pydantic"
    assert native.database_schema == "moyai_native_test"
    assert native.temporal_queue == "isolated-pydantic-v1"
    assert native.temporal_tls is True
    assert native.artifact_prefix == "pydantic-v1"
    assert native.artifact_session_token.get_secret_value() == "synthetic-session"
    assert not Path(environment["DATA_DIR"]).exists()
    for secret in ("synthetic-secret", environment["ENCRYPTION_KEY"], "synthetic:password"):
        assert secret not in repr(config)


@pytest.mark.parametrize("selected", ["", "astra", "sol", "synthetic/custom-model"])
def test_product_model_aliases_and_default_catalog_are_preserved(environment, selected):
    environment["AGENT_MODEL"] = selected
    config = configured(environment)
    source = config.legacy_settings()
    native = Settings(_env_file=None, **config.native_overrides())
    assert native.default_model == source.resolve_model()
    assert set(native.models) == {source.resolve_model(value) for value in source.allowed_models()}


@pytest.mark.parametrize(("name", "value"), [
    ("MOYAI_CORE", "unknown"),
    ("MOYAI_PRODUCT_MODE", "production"),
    ("MOYAI_RUNTIME_ROLE", "all"),
    ("MOYAI_RUNTIME_ROLE", "standalone"),
    ("MOYAI_DATABASE_URL", "sqlite:///accidental.db"),
    ("MOYAI_DATABASE_URL", "postgresql:///missing-host"),
    ("MOYAI_DATABASE_URL", "postgresql://localhost"),
    ("MOYAI_DATABASE_URL", "postgresql://localhost:invalid/db"),
    ("MOYAI_DATABASE_URL", "postgresql://localhost/db\n"),
    ("MOYAI_NATIVE_SCHEMA", "moyai"),
    ("MOYAI_NATIVE_SCHEMA", "pg_private"),
    ("MOYAI_NATIVE_SCHEMA", "public"),
    ("MOYAI_NATIVE_SCHEMA", "native;DROP TABLE users"),
    ("MOYAI_SCHEMA_MODE", "auto"),
    ("MOYAI_NATIVE_SCHEMA_MODE", "bootstrap"),
    ("MOYAI_DATABASE_INITIALIZE", "true"),
    ("TEMPORAL_ENABLED", "false"),
    ("MOYAI_COORDINATOR_HANDOFF", "false"),
    ("MOYAI_SEPARATE_BROKER", "false"),
    ("ENCRYPTION_KEY", "incorrect-not-raw-fernet"),
    ("MOYAI_NATIVE_ENCRYPTION_KEY", "too-short"),
    ("MOYAI_NATIVE_KEY_FORMAT", "raw-fernet"),
    ("MOYAI_NATIVE_OBJECT_FORMAT", "raw-sha256"),
    ("MOYAI_NATIVE_OBJECT_PREFIX", "../escape"),
    ("MOYAI_NATIVE_OBJECT_PREFIX", "moyai"),
    ("MOYAI_NATIVE_OBJECT_PREFIX", "moyai/native"),
    ("MOYAI_NATIVE_TASK_QUEUE", "existing-sessions-v1"),
    ("MOYAI_NATIVE_TASK_QUEUE", "existing-sessions-v1-agents-v1"),
    ("MOYAI_NATIVE_TASK_QUEUE", "unsafe queue"),
    ("OBJECT_STORAGE_ENDPOINT", "https://user:secret@example.test"),
    ("OBJECT_STORAGE_SECRET_ACCESS_KEY", ""),
    ("MOYAI_PUBLIC_URL", "https://user:secret@example.test"),
    ("MOYAI_PUBLIC_URL", "https://example.test/path"),
    ("MOYAI_PRODUCT_LOCAL_REHEARSAL", "yes"),
])
def test_invalid_config_fails_without_creating_data_or_echoing_values(environment, name, value):
    environment[name] = value
    with pytest.raises(ProductConfigurationError) as caught:
        configured(environment)
    assert "synthetic-secret" not in str(caught.value)
    assert "synthetic:password" not in str(caught.value)
    assert "user:secret" not in str(caught.value)
    assert not Path(environment["DATA_DIR"]).exists()


@pytest.mark.parametrize("name", [
    "MOYAI_DATABASE_URL", "MOYAI_DATABASE_SCHEMA", "MOYAI_NATIVE_SCHEMA",
    "SESSION_SECRET", "ENCRYPTION_KEY", "MOYAI_NATIVE_ENCRYPTION_KEY",
    "MOYAI_NATIVE_KEY_FORMAT", "MOYAI_NATIVE_OBJECT_FORMAT", "MOYAI_NATIVE_OBJECT_PREFIX",
    "MOYAI_NATIVE_TASK_QUEUE", "OBJECT_STORAGE_BUCKET", "OBJECT_STORAGE_PREFIX",
    "TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "MOYAI_DATABASE_INITIALIZE",
])
def test_missing_explicit_integration_fields_never_fall_back(environment, name):
    environment.pop(name)
    environment["DATABASE_URL"] = "postgresql://fallback.invalid/forbidden"
    with pytest.raises(ProductConfigurationError):
        configured(environment)
    assert not Path(environment["DATA_DIR"]).exists()


@pytest.mark.parametrize("role", ["api", "coordinator", "broker", "worker"])
def test_reviewed_production_roles_reach_runtime_only_after_dependency_check(environment, monkeypatch, role):
    from moyai import product_start

    class RuntimeBoundary(Exception):
        pass

    checked = []

    def runtime_boundary(_):
        assert checked == ["dependencies"]
        raise RuntimeBoundary()

    environment["MOYAI_RUNTIME_ROLE"] = role
    config = configured(environment)
    monkeypatch.setattr(product_start, "check_runtime_dependencies", lambda: checked.append("dependencies"))
    monkeypatch.setattr(product_start, "_runtime", runtime_boundary)
    with pytest.raises(RuntimeBoundary):
        product_start.create_application(config)
    plan = build_role_plan(config)
    assert not plan.blockers and plan.serve_api
    if role == "api":
        assert not plan.run_dispatcher and not plan.run_worker
    assert plan.run_worker == (role == "worker")
    assert plan.run_dispatcher == (role == "coordinator")


def test_local_rehearsal_is_explicit_loopback_and_owns_only_native_execution(environment):
    environment.update(
        MOYAI_PRODUCT_LOCAL_REHEARSAL="true", MOYAI_RUNTIME_ROLE="standalone",
        MOYAI_COORDINATOR_HANDOFF="false", MOYAI_SEPARATE_BROKER="false",
    )
    config = configured(environment)
    plan = require_startable(config)
    assert plan.serve_api and plan.run_worker and plan.run_dispatcher
    assert config.native_schema_mode == "verify"
    from moyai.product_start import _check_listener
    _check_listener(config, "127.0.0.1")
    with pytest.raises(ProductDeploymentError, match="loopback"):
        _check_listener(config, "0.0.0.0")


@pytest.mark.parametrize(("name", "value"), [
    ("MOYAI_DATABASE_URL", "postgresql://private.example.test/database"),
    ("MOYAI_DATABASE_URL", "postgresql://127.0.0.1/database?host=private.example.test"),
    ("TEMPORAL_ADDRESS", "private.example.test:7233"),
    ("MOYAI_PUBLIC_URL", "https://private.example.test"),
    ("OBJECT_STORAGE_ENDPOINT", ""),
    ("OBJECT_STORAGE_ENDPOINT", "https://s3.example.test"),
    ("RENDER_SERVICE_ID", "synthetic-render-service"),
    ("RENDER_EXTERNAL_URL", "https://render.example.test"),
])
def test_rehearsal_does_not_disable_remote_or_render_boundaries(environment, name, value):
    environment["MOYAI_PRODUCT_LOCAL_REHEARSAL"] = "true"
    environment[name] = value
    with pytest.raises(ProductConfigurationError, match="rehearsal"):
        configured(environment)


def test_source_core_and_queue_changes_are_not_rolling_equivalent(environment):
    config = configured(environment)
    original = candidate_role_fingerprint(config)
    for name, value in (
        ("MOYAI_NATIVE_SCHEMA", "moyai_other_native"),
        ("MOYAI_NATIVE_TASK_QUEUE", "native-other-v1"),
        ("MOYAI_NATIVE_ENCRYPTION_KEY", "different-synthetic-secret-with-32-characters"),
        ("MOYAI_NATIVE_OBJECT_PREFIX", "native-other"),
        ("SESSION_SECRET", "different-product-session-key"),
    ):
        assert candidate_role_fingerprint(configured({**environment, name: value})) != original


def test_native_product_extra_imports_real_product_modules_without_old_agent_sdk():
    # Exercise the actual installed native environment, not import stubs.
    versions = check_runtime_dependencies()
    assert versions["openai"] == "3.29.0"
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json,sys; from moyai.product_deployment import check_runtime_dependencies; "
            "v=check_runtime_dependencies(); "
            "assert not any(n in sys.modules for n in ('app.main','agents','app.agent_runtime')); "
            "print(json.dumps(v))"
        )],
        cwd=ROOT, env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == versions


def test_root_selector_preserves_legacy_and_uses_separate_native_interpreter(monkeypatch):
    import docker_start

    monkeypatch.setattr(os, "environ", {"PORT": "12345"})
    legacy = docker_start.server_command()
    assert legacy[0] == sys.executable and "app.main:app" in legacy
    monkeypatch.setenv("MOYAI_CORE", "pydantic")
    native = docker_start.server_command()
    assert native[:4] == [str(ROOT / "native/.venv/bin/python"), "-m", "moyai.product_start", "serve"]
    assert native[-1] == "12345"
    monkeypatch.setenv("RENDER_SERVICE_ID", "synthetic")
    rendered = docker_start.server_command()
    assert rendered[0] == str(ROOT / "native/.venv/bin/python")
    assert Path(rendered[1]).resolve() == ROOT / "render_start.py"
    monkeypatch.setenv("MOYAI_CORE", "typo")
    with pytest.raises(ValueError):
        docker_start.server_command()


def test_missing_native_environment_never_falls_back_to_legacy(monkeypatch, tmp_path):
    import docker_start

    monkeypatch.setattr(docker_start, "__file__", str(tmp_path / "docker_start.py"))
    monkeypatch.setattr(os, "environ", {"MOYAI_CORE": "pydantic"})
    with pytest.raises(RuntimeError, match="locked native interpreter"):
        docker_start.server_command()


def test_render_native_selection_does_not_call_legacy_bootstrap(environment, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import docker_start
    import render_start

    from moyai import product_start

    monkeypatch.setattr(os, "environ", {**environment, "RENDER_MIGRATION_STAGE": "false",
                                      "MOYAI_PUBLIC_URL": "https://synthetic.example.test"})
    monkeypatch.setattr(docker_start, "ensure_selected_interpreter", lambda: None)
    bootstrap = AsyncMock()
    monkeypatch.setattr(render_start, "bootstrap", bootstrap)
    application = SimpleNamespace(state=SimpleNamespace(product_lifecycle=None))
    calls = []
    monkeypatch.setattr(product_start, "create_application", lambda *a, **kw: application)

    class Server:
        def __init__(self, config, **kwargs):
            assert config.app is application
            calls.append("native")

        def run(self):
            calls.append("served")

    monkeypatch.setattr(render_start, "RenderOverlapServer", Server)
    render_start.main()
    assert calls == ["native", "served"]
    bootstrap.assert_not_awaited()


@pytest.mark.parametrize("command", ["user", "dev", "serve", "worker"])
async def test_direct_cli_cannot_bypass_production_role_or_identity_guard(environment, monkeypatch, command):
    from argparse import Namespace

    from moyai import cli

    monkeypatch.setattr(os, "environ", environment.copy())
    monkeypatch.setattr(cli, "load_dotenv", lambda **_: None)
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(cli, "Runtime", lambda *a, **kw: pytest.fail("No resources before admission"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: pytest.fail("No native product account"))
    args = Namespace(command=command, data_dir=None, host="127.0.0.1", port=8807)
    with pytest.raises(ProductDeploymentError):
        await cli.run(args)
    assert not Path(environment["DATA_DIR"]).exists()


@pytest.mark.parametrize(("command", "host", "role"), [
    ("serve", "127.0.0.1", "standalone"),
    ("worker", "127.0.0.1", "standalone"),
    ("dev", "0.0.0.0", "standalone"),
    ("dev", "127.0.0.1", "api"),
])
async def test_direct_existing_cli_only_allows_loopback_standalone_dev(
    environment, monkeypatch, command, host, role,
):
    from argparse import Namespace

    from moyai import cli

    environment.update(MOYAI_PRODUCT_LOCAL_REHEARSAL="true", MOYAI_RUNTIME_ROLE=role)
    if role == "standalone":
        environment.update(MOYAI_COORDINATOR_HANDOFF="false", MOYAI_SEPARATE_BROKER="false")
    monkeypatch.setattr(os, "environ", environment.copy())
    monkeypatch.setattr(cli, "load_dotenv", lambda **_: None)
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(cli, "Runtime", lambda *a, **kw: pytest.fail("No resources before admission"))
    with pytest.raises(ProductDeploymentError):
        await cli.run(Namespace(command=command, data_dir=None, host=host, port=8807))


async def test_cli_dev_uses_effective_native_settings_and_creates_only_its_local_state(
    environment, monkeypatch,
):
    from argparse import Namespace
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from moyai import cli

    environment.update(
        MOYAI_PRODUCT_LOCAL_REHEARSAL="true", MOYAI_RUNTIME_ROLE="standalone",
        MOYAI_COORDINATOR_HANDOFF="false", MOYAI_SEPARATE_BROKER="false",
    )
    monkeypatch.setattr(os, "environ", environment.copy())
    monkeypatch.setattr(cli, "load_dotenv", lambda **_: None)
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None))
    config = configured(environment)
    native = Settings(_env_file=None, **config.native_overrides())
    runtime = SimpleNamespace(
        settings=native, start=AsyncMock(), close=AsyncMock(), security=object(), objects=object(),
    )

    def construct(settings, *, product_config):
        assert settings.data_dir == Path(environment["DATA_DIR"])
        assert product_config.native_task_queue == config.native_task_queue
        return runtime

    class LocalEnvironment:
        client = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def start_local(**kwargs):
        assert native.data_dir.is_dir()
        assert native.data_dir.stat().st_mode & 0o777 == 0o700
        assert kwargs["dev_server_database_filename"] == str(native.data_dir / "temporal.db")
        assert kwargs["namespace"] == config.legacy_settings().temporal_namespace
        return LocalEnvironment()

    @asynccontextmanager
    async def worker(selected):
        assert selected is runtime
        assert selected.settings.temporal_queue != environment["TEMPORAL_TASK_QUEUE"]
        assert selected.settings.encryption_key.get_secret_value() == environment["MOYAI_NATIVE_ENCRYPTION_KEY"]
        yield

    monkeypatch.setattr(cli, "Runtime", construct)
    monkeypatch.setattr(cli.WorkflowEnvironment, "start_local", start_local)
    monkeypatch.setattr(cli, "make_worker", worker)
    monkeypatch.setattr(cli, "encrypted_converter", lambda *_: object())
    monkeypatch.setattr(cli, "create_app", lambda _: FastAPI())
    serve = AsyncMock()
    monkeypatch.setattr(cli.uvicorn, "Server", lambda _: SimpleNamespace(serve=serve))
    await cli.run(Namespace(command="dev", data_dir=None, host="127.0.0.1", port=8807))
    runtime.start.assert_awaited_once()
    runtime.close.assert_awaited_once()
    serve.assert_awaited_once()
    assert sorted(item.name for item in Path(environment["DATA_DIR"]).iterdir()) == ["pydantic"]


@pytest.fixture
def isolated_postgres(environment):
    """Only create/drop a random database on an explicitly enabled loopback PG."""
    import psycopg
    from psycopg import sql

    base = os.getenv("MOYAI_TEST_POSTGRES")
    if not base:
        pytest.skip("Set MOYAI_TEST_POSTGRES to a disposable local PostgreSQL URL.")
    parsed = urlsplit(base)
    assert parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    name = "moyai_deploy_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            environment["MOYAI_DATABASE_URL"] = urlunsplit(parsed._replace(path="/" + name))
            yield environment
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_real_product_schema_receipt_verification_is_read_only(isolated_postgres, monkeypatch):
    import app.schema_migrations
    import psycopg
    from app.schema_migrations import SCHEMA_REVISION

    config = configured(isolated_postgres)
    with pytest.raises(ProductDeploymentError, match="schema verification failed"):
        verify_product_schema(config)
    with psycopg.connect(config.database_url, autocommit=True) as connection:
        assert connection.execute("SELECT to_regnamespace('moyai')").fetchone()[0] is None
        connection.execute("CREATE SCHEMA moyai")
        connection.execute("CREATE TABLE moyai.schema_state(id INTEGER PRIMARY KEY, revision INTEGER, dirty INTEGER)")
        connection.execute("INSERT INTO moyai.schema_state VALUES(1,%s,0)", (SCHEMA_REVISION,))
        assert verify_product_schema(config)["required"] == SCHEMA_REVISION
        connection.execute("UPDATE moyai.schema_state SET dirty=1")
        with pytest.raises(ProductDeploymentError):
            verify_product_schema(config)
        connection.execute("UPDATE moyai.schema_state SET dirty=0")

        def forbidden_ddl(conn, schema, mode):
            conn.execute("CREATE TABLE moyai.forbidden_write(id INTEGER)")

        monkeypatch.setattr(app.schema_migrations, "prepare_schema", forbidden_ddl)
        with pytest.raises(ProductDeploymentError):
            verify_product_schema(config)
        assert connection.execute("SELECT to_regclass('moyai.forbidden_write')").fetchone()[0] is None


async def test_explicit_native_provision_and_verify_use_real_postgres_stores(isolated_postgres):
    import psycopg
    from app.schema_migrations import SCHEMA_REVISION

    from moyai.product_start import prepare_native_schema

    config = configured(isolated_postgres)
    with psycopg.connect(config.database_url, autocommit=True) as connection:
        connection.execute("CREATE SCHEMA moyai")
        # This schema unit test supplies the source contract touched by the
        # continuation fence. Full canonical migration runs in the separate
        # legacy interpreter during release/app qualification.
        connection.execute("CREATE TABLE moyai.schema_state(id INTEGER PRIMARY KEY, revision INTEGER, dirty INTEGER)")
        connection.execute("INSERT INTO moyai.schema_state VALUES(1,%s,0)", (SCHEMA_REVISION,))
        connection.execute("CREATE TABLE moyai.runs(id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE moyai.messages(id BIGINT PRIMARY KEY,run_id TEXT)")
        connection.execute("CREATE TABLE moyai.durable_sessions(run_id TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE moyai.runtime_leases(name TEXT PRIMARY KEY,token TEXT,expires_at TIMESTAMPTZ)"
        )
        connection.execute("CREATE TABLE moyai.run_roots(run_id TEXT PRIMARY KEY,root_id TEXT)")
    with pytest.raises(ProductDeploymentError):
        await prepare_native_schema(config, apply=False)
    result = await prepare_native_schema(config, apply=True)
    assert result["harness_schema"] == "provisioned"
    assert result["deployment_readiness"] == "not_checked"
    assert (await prepare_native_schema(config, apply=False))["harness_schema"] == "verified"
    with psycopg.connect(config.database_url, autocommit=True) as connection:
        # SDK provision probes must not create a user, turn, memory file or plan.
        for table in ("moyai_users", "moyai_turns", "agent_memory", "plan_items"):
            assert connection.execute(f'SELECT count(*) FROM "{config.native_schema}"."{table}"').fetchone()[0] == 0
        connection.execute(f'DROP TABLE "{config.native_schema}".agent_memory')
    with pytest.raises(ProductDeploymentError):
        await prepare_native_schema(config, apply=False)
    with psycopg.connect(config.database_url) as connection:
        assert connection.execute(
            "SELECT to_regclass(%s)", (f"{config.native_schema}.agent_memory",),
        ).fetchone()[0] is None
