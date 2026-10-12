"""Actual PostgreSQL ownership and HTTP identity checks for API releases."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from app import main
from scripts.release.api_deploy import candidate_contract
from scripts.release.api_probe import probe, snapshot
from scripts.release.deploy import Service
from test_api_lifecycle import cluster, client_for  # noqa: F401
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401


def state(settings):
    with psycopg.connect(settings.moyai_database_url) as connection:
        return snapshot(connection, settings.moyai_database_schema)


def environment(settings):
    # Use real Settings rather than encoding a fingerprint independently.
    return {key.upper(): str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in settings.model_dump().items() if isinstance(value, (str, bool, int, float, Path))}


def test_snapshot_is_read_only_and_tracks_owner_replacement_without_count_change(cluster):
    _, settings = cluster
    with ExitStack() as lifetime:
        for role in ('broker', 'worker'):
            app = main.create_app(settings.model_copy(update={'moyai_runtime_role': role}))
            lifetime.callback(app.state.store.close)
        api = main.create_app(settings)
        with client_for(api) as client:
            response = client.get('/health')
            assert response.json() == {'status': 'ok'}
            assert response.headers['x-moyai-build'] == settings.moyai_build_sha
            assert response.headers['x-moyai-api-release'] == '1'
            pid = str(api.state.store.database.owner_pid)
            assert response.headers['x-moyai-owner'] == pid
            before = state(settings)
            assert len(before['owners']) == 4
            identity = next(o for o in before['owners'] if o.split('@')[0] == pid)
            with psycopg.connect(settings.moyai_database_url) as connection:
                snapshot(connection, settings.moyai_database_schema)
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    connection.execute('DELETE FROM runtime_api_policy')
            api.state.manager.ready.clear()
            unavailable = client.get('/health')
            assert unavailable.status_code == 503 and unavailable.headers['x-moyai-owner'] == pid
        with client_for(main.create_app(settings.model_copy(update={'moyai_build_sha': 'b' * 40}))) as replacement:
            after = state(settings)
            assert len(after['owners']) == 4 and identity not in after['owners']
            assert after['coordinators'] == before['coordinators'] and after['brokers'] == before['brokers']
            assert after['contract'] == before['contract']
            assert replacement.get('/health').headers['x-moyai-build'] == 'b' * 40


def test_candidate_contract_uses_effective_settings_and_isolated_environment(cluster, monkeypatch):
    _, settings = cluster
    env = environment(settings)
    # Render requires an HTTPS origin; no actual network is used by this child.
    env['MOYAI_PUBLIC_URL'] = 'https://moyai.example'
    monkeypatch.setenv('SESSION_SECRET', 'runner-value-must-not-enter-candidate')
    monkeypatch.setenv('MAX_CONCURRENT_RUNS', '999999')
    original = candidate_contract(env)
    assert original == candidate_contract({**env, 'MOYAI_BUILD_SHA': 'b' * 40})
    assert original != candidate_contract({**env, 'MAX_CONCURRENT_RUNS': '23'})
    with pytest.raises(Exception) as error:
        candidate_contract({**env, 'MAX_CONCURRENT_RUNS': 'secret-invalid-setting'})
    assert 'secret-invalid-setting' not in str(error.value)


@pytest.mark.parametrize('failure', [None, 'unhealthy', 'wrong_build', 'wrong_pid', 'missing_version', 'stale_contract'])
def test_remote_probe_correlates_http_process_to_actual_database_owner(cluster, failure):
    _, settings = cluster
    settings = settings.model_copy(update={'public_url': 'https://127.0.0.1:8787'})
    # Re-publish fixtures with the same new origin before starting the API.
    coordinator, _ = cluster
    coordinator.state.store.close()
    coordinator = main.create_app(settings.model_copy(update={'moyai_runtime_role': 'coordinator'}))
    try:
        with client_for(main.create_app(settings)) as client:
            response = client.get('/health')
            headers = dict(response.headers)
            headers = {key.title(): value for key, value in headers.items()}
            # urllib headers are case-insensitive, as on the real HTTP wire.
            from email.message import Message
            wire_headers = Message()
            for key, value in headers.items(): wire_headers[key] = value
            if failure == 'wrong_build': wire_headers.replace_header('X-Moyai-Build', 'c' * 40)
            if failure == 'wrong_pid': wire_headers.replace_header('X-Moyai-Owner', '0')
            if failure == 'missing_version': del wire_headers['X-Moyai-Api-Release']
            env = environment(settings)
            env.update(MOYAI_PUBLIC_URL=settings.public_url, RENDER_GIT_COMMIT=settings.moyai_build_sha,
                       RENDER_MIGRATION_STAGE='false', MAINTENANCE_DRAIN='false')
            service = Service('srv-fixture', 'api', env, env.copy(), settings.moyai_build_sha, 'dep-fixture')
            body = io.BytesIO(json.dumps({'status': 'ok'}).encode())
            body.status, body.headers = (503 if failure == 'unhealthy' else 200), wire_headers
            if failure == 'stale_contract':
                with psycopg.connect(settings.moyai_database_url, autocommit=True) as conn:
                    conn.execute(f'UPDATE "{settings.moyai_database_schema}".runtime_api_policy SET fingerprint=\'stale\'')
            with patch.dict(os.environ, env, clear=True), patch('urllib.request.urlopen', return_value=body):
                if failure not in (None, 'unhealthy'):
                    with pytest.raises(ValueError): probe(service.probe_options())
                else:
                    result = probe(service.probe_options())
                    assert result['ok'] is True
                    assert result['ready'] == (failure is None)
                    assert result['api_owner'].split('@')[0] == response.headers['x-moyai-owner']
    finally:
        coordinator.state.store.close()
