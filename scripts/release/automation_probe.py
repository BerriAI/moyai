"""Read-only confirmation of the original queue's tracked schedule pauses.

This runs in legacy Python and never starts, pauses, cancels or terminates a
Temporal workflow. A pause receipt is not a claim that control histories ended.
"""
import asyncio
from datetime import timedelta
import hashlib
import json
import sys

LIMIT = 1000


def require(condition):
    if not condition:
        raise ValueError('The source automation pause could not be verified.')


def inventory(connection, schema, counts_fn):
    from psycopg import sql
    with connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        connection.execute("SET LOCAL statement_timeout='5s'")
        connection.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        counts = counts_fn(connection)
        require(counts['legacy_automations'] == 0)
        definitions = connection.execute(
            "SELECT id,revision,synced_revision,paused,synced_schedule_version,sync_error "
            "FROM automations ORDER BY id LIMIT %s", (LIMIT + 1,)
        ).fetchall()
        schedules = connection.execute(
            'SELECT automation_id,schedule_id FROM automation_schedules ORDER BY automation_id,schedule_id LIMIT %s',
            (LIMIT + 1,),
        ).fetchall()
        require(len(definitions) <= LIMIT and len(schedules) <= LIMIT
                and len(schedules) == counts['legacy_schedule_count']
                and all(isinstance(row[1], str) and row[1] for row in schedules))
        return definitions, schedules


async def confirm_paused(settings, schedules, *, connect=None):
    if not schedules:
        return
    from temporalio.client import Client, ScheduleActionStartWorkflow
    connect = connect or Client.connect
    async with asyncio.timeout(75):
        client = await connect(settings.temporal_address, namespace=settings.temporal_namespace,
            api_key=settings.temporal_api_key or None, tls=settings.temporal_tls)
        limit = asyncio.Semaphore(8)

        async def check(identity):
            async with limit:
                description = await client.get_schedule_handle(identity).describe(rpc_timeout=timedelta(seconds=10))
                action = description.schedule.action
                require(description.schedule.state.paused is True
                        and isinstance(action, ScheduleActionStartWorkflow)
                        and action.workflow == 'AutomationWorkflow'
                        and action.task_queue == settings.temporal_task_queue)
        await asyncio.gather(*(check(row[1]) for row in schedules))


def execute(options, *, probe_fn, counts_fn):
    import psycopg
    from app.config import Settings
    require(options['role'] == 'coordinator')
    before = probe_fn(options)
    require(before.get('ok') is True and before.get('ready') is True
            and before.get('legacy_automations') == 0)
    if options['staged']:
        require(before.get('identity') is None and all(
            before.get(key) == [] for key in ('owners', 'coordinators', 'brokers', 'broker_leaders')))
    settings = Settings(_env_file=None)
    with psycopg.connect(settings.moyai_database_url, connect_timeout=10, autocommit=True) as connection:
        original = inventory(connection, settings.moyai_database_schema, counts_fn)
        require(len(original[1]) == before['legacy_schedule_count'])
        asyncio.run(confirm_paused(settings, original[1]))
        require(inventory(connection, settings.moyai_database_schema, counts_fn) == original)
    after = probe_fn(options)
    require(after.get('ok') is True and after.get('ready') is True
            and after.get('legacy_automations') == 0)
    require(all(after.get(key) == before.get(key) for key in (
        'identity', 'policy', 'contracts', 'source_policy', 'source_contracts',
        'schema', 'staged', 'core', 'legacy_schedule_count',
    )))
    return {'status': 'remote_schedules_paused', 'count': len(original[1]),
            'digest': hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()}


class SSHAutomationProbe:
    def __init__(self, key, *, connections=None):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'automation_probe.py', connections=connections)

    def __call__(self, service):
        from scripts.release.deploy import ReleaseError
        from scripts.release.maintenance_probe import piped_source
        source = ("_probe = {'__name__': 'source_automation_probe'}\n"
                  'exec(' + repr(piped_source()) + ', _probe)\n' + self.transport.source.read_text())
        value = self.transport.run(service, service.probe_options(), marker='MOYAI_AUTOMATION_PROBE=',
                                   timeout=110, source=source, read_only=True)
        if not valid_receipt(value):
            raise ReleaseError('Source Temporal schedule pauses were not verified; no control workflow was cancelled or replayed.')
        return value


def valid_receipt(value):
    import re
    return (isinstance(value, dict) and set(value) == {'status', 'count', 'digest'}
        and value['status'] == 'remote_schedules_paused' and type(value['count']) is int
        and 0 <= value['count'] <= LIMIT and isinstance(value['digest'], str)
        and re.fullmatch('[0-9a-f]{64}', value['digest']) is not None)


if __name__ == '__main__':
    try:
        value = execute(json.loads(sys.argv[1]), probe_fn=_probe['probe'],
                        counts_fn=_probe['legacy_automation_counts'])
    except Exception:
        value = {'status': 'unconfirmed'}
    print('MOYAI_AUTOMATION_PROBE=' + json.dumps(value, sort_keys=True))
