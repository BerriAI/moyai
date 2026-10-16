import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from sqlalchemy import text

from moyai.deployment_bootstrap import installation_lock
from moyai.deployment_config import InstallationError
from moyai.postgres import create_engine


async def test_standard_postgres_uri_options_survive_pool_and_reconstructed_engine():
    uri = os.environ.get("MOYAI_TEST_POSTGRES", "")
    if not uri:
        pytest.skip("Set MOYAI_TEST_POSTGRES to an isolated PostgreSQL database.")
    parsed = urlsplit(uri)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("The plaintext-TLS-mode regression uses only a loopback test database.")
    query = dict(parse_qsl(parsed.query))
    query.update(sslmode="disable", application_name="moyai-package-uri-test")
    uri = urlunsplit(parsed._replace(query=urlencode(query)))
    engine = create_engine(uri, connect_args={"server_settings": {"statement_timeout": "5000"}})
    reconstructed = create_engine(engine.url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_setting('application_name')")) == "moyai-package-uri-test"
            assert await connection.scalar(text("SELECT current_setting('statement_timeout')")) == "5s"
        async with reconstructed.connect() as connection:
            assert await connection.scalar(text("SELECT current_setting('application_name')")) == "moyai-package-uri-test"
    finally:
        await engine.dispose()
        await reconstructed.dispose()


async def test_installation_mutex_spans_independent_connections():
    uri = os.environ.get("MOYAI_TEST_POSTGRES", "")
    if not uri:
        pytest.skip("Set MOYAI_TEST_POSTGRES to an isolated PostgreSQL database.")
    values = {"MOYAI_DATABASE_URL": uri, "MOYAI_NATIVE_DATABASE_SCHEMA": "moyai_install_lock_test"}
    async with installation_lock(values):
        with pytest.raises(InstallationError, match="Another installer"):
            async with installation_lock(values):
                pytest.fail("A concurrent installer acquired the namespace.")
    async with installation_lock(values):
        pass
