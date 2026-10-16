"""Source schedule confirmation is read-only and does not imply closed histories."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg import sql
import pytest
from temporalio.client import Schedule, ScheduleActionStartWorkflow, ScheduleSpec, ScheduleState

from scripts.release import automation_probe
from scripts.release.maintenance_probe import legacy_automation_counts
from scripts.release.deploy import COORDINATOR, ReleaseError, Service
from postgres_fixture import target  # noqa: F401


@pytest.mark.parametrize('fault', [None, 'unpaused', 'queue', 'workflow', 'missing'])
def test_exact_remote_schedule_state_and_queue_are_required(fault):
    action = ScheduleActionStartWorkflow('OtherWorkflow' if fault == 'workflow' else 'AutomationWorkflow',
        id='test-tick', task_queue='other' if fault == 'queue' else 'original-queue')
    description = SimpleNamespace(schedule=Schedule(action=action, spec=ScheduleSpec(),
        state=ScheduleState(paused=fault != 'unpaused')))
    calls = []
    class Handle:
        async def describe(self, **kwargs):
            calls.append(('describe', kwargs))
            if fault == 'missing':
                raise RuntimeError('Synthetic missing schedule')
            return description
    class Client:
        def get_schedule_handle(self, identity):
            assert identity == 'moyai-automation-fixture'
            return Handle()
    async def connect(*args, **kwargs):
        return Client()
    settings = SimpleNamespace(temporal_address='127.0.0.1:1', temporal_namespace='default',
        temporal_api_key='', temporal_tls=False, temporal_task_queue='original-queue')
    operation = automation_probe.confirm_paused(settings, [('id', 'moyai-automation-fixture')], connect=connect)
    if fault:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(operation)
    else:
        asyncio.run(operation)
    assert len(calls) == 1  # No mutating SDK method exists on the supplied handle.


@pytest.fixture
def source(target, monkeypatch):
    from app.automation_definitions import SCHEDULE_VERSION
    url, schema = target
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        connection.execute('''CREATE TABLE automations(id text,revision integer,synced_revision integer,
            paused integer,synced_schedule_version integer,sync_error text)''')
        connection.execute('INSERT INTO automations VALUES(%s,2,2,1,%s,%s)', ('fixture', SCHEDULE_VERSION, ''))
        connection.execute('CREATE TABLE automation_schedules(automation_id text,schedule_id text)')
        connection.execute("INSERT INTO automation_schedules VALUES('fixture','moyai-automation-fixture')")
        monkeypatch.setattr('app.config.Settings', lambda **kwargs: SimpleNamespace(
            moyai_database_url=url, moyai_database_schema=schema))
        yield connection, schema


@pytest.mark.parametrize('fault', [None, 'metadata_drift', 'owner_drift', 'missing_owner', 'unpaused'])
def test_pause_confirmation_is_bounded_by_readonly_metadata_and_exact_source_identity(source, monkeypatch, fault):
    connection, _ = source
    calls = []
    state = {'ok': True, 'ready': True, 'legacy_automations': 0, 'legacy_schedule_count': 1,
             'identity': '1@source', 'policy': 'p', 'contracts': {}, 'source_policy': 'p',
             'source_contracts': {}, 'schema': 20, 'staged': False, 'core': 'legacy'}
    def probe(options):
        observed = deepcopy(state)
        if calls and fault == 'owner_drift':
            observed['identity'] = '2@replacement'
        if calls and fault == 'missing_owner':
            observed['ready'] = False
        return observed
    async def confirm(settings, rows):
        calls.append(rows)
        if fault == 'metadata_drift':
            connection.execute('UPDATE automations SET revision=3,synced_revision=3')
        if fault == 'unpaused':
            connection.execute('UPDATE automations SET paused=0')
    monkeypatch.setattr(automation_probe, 'confirm_paused', confirm)
    operation = lambda: automation_probe.execute({'role': 'coordinator', 'staged': False},
        probe_fn=probe, counts_fn=legacy_automation_counts)
    if fault:
        with pytest.raises(ValueError):
            operation()
    else:
        result = operation()
        assert automation_probe.valid_receipt(result) and result['count'] == 1
        assert 'fixture' not in str(result)
    assert len(calls) == 1


def test_staging_requires_every_source_owner_retired(source, monkeypatch):
    async def unexpected(*args):
        pytest.fail('Queried Temporal before the zero-owner boundary.')
    monkeypatch.setattr(automation_probe, 'confirm_paused', unexpected)
    state = {'ok': True, 'ready': True, 'legacy_automations': 0, 'identity': None,
             'owners': ['still-live'], 'coordinators': [], 'brokers': [], 'broker_leaders': []}
    with pytest.raises(ValueError):
        automation_probe.execute({'role': 'coordinator', 'staged': True},
            probe_fn=lambda _: state, counts_fn=legacy_automation_counts)


def test_ssh_probe_uses_legacy_bundle_and_readonly_transport(monkeypatch):
    env = {'MOYAI_RUNTIME_ROLE': 'coordinator', 'RENDER_MIGRATION_STAGE': 'true', 'MAINTENANCE_DRAIN': 'false'}
    service = Service(COORDINATOR, 'coordinator', env, deepcopy(env), 'a' * 40, 'dep-source')
    receipt = {'status': 'remote_schedules_paused', 'count': 1, 'digest': 'b' * 64}
    probe = automation_probe.SSHAutomationProbe(Path('/synthetic-key'))
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        return receipt
    monkeypatch.setattr(probe.transport, 'run', run)
    assert probe(service) == receipt
    assert calls[0]['read_only'] is True and calls[0]['timeout'] == 110
    assert 'native/.venv' not in calls[0]['source'] and 'legacy_automation_counts' in calls[0]['source']
    receipt['status'] = 'unconfirmed'
    with pytest.raises(ReleaseError):
        probe(service)
