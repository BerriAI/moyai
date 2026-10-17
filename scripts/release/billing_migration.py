"""Prepared billing cutover at the native maintenance zero-owner boundary."""
import asyncio
import json
import os
import sys

SOURCES = {'legacy', 'native'}


def require(condition):
    if not condition:
        raise ValueError('Billing consolidation prerequisites or receipt changed.')


def validate(value, *, source_schema, native_schema, consolidated=False):
    require(isinstance(value, dict) and set(value) == {
        'source_schema', 'native_schema', 'version', 'phase', 'physical_billing_stores',
        'backfill', 'records', 'comparison'})
    require(value['source_schema'] == source_schema and value['native_schema'] == native_schema)
    require(type(value['version']) is int and value['version'] == 1)
    require(value['phase'] in {'shadow', 'active', 'consolidated'}
            and (not consolidated or value['phase'] == 'consolidated'))
    require(type(value['physical_billing_stores']) is int
            and value['physical_billing_stores'] == (1 if value['phase'] == 'consolidated' else 3))
    require(value['backfill'] == {'legacy': True, 'native': True}
            and all(type(flag) is bool for flag in value['backfill'].values()))
    require(isinstance(value['records'], dict) and set(value['records']) == SOURCES
            and all(type(count) is int and count >= 0 for count in value['records'].values()))
    require(isinstance(value['comparison'], dict) and set(value['comparison']) == SOURCES)
    for source, row in value['comparison'].items():
        require(isinstance(row, dict) and set(row) == {'source_rows', 'mismatches', 'extra_rows'}
                and all(type(count) is int and count >= 0 for count in row.values())
                and row['source_rows'] == value['records'][source]
                and row['mismatches'] == row['extra_rows'] == 0)
    return value


async def observe_storage(engine, storage):
    from sqlalchemy import text
    from moyai.billing_storage import _compare, _state, _timeouts, verify_storage
    from moyai.billing_storage_cutover import _source_spec

    # Mirrors commit with source writes, so all counts/comparisons belong to one
    # consistent snapshot even while the source application is still running.
    async with engine.connect() as connection:
        await connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        await _timeouts(connection)
        state = await _state(connection, storage)
        await verify_storage(connection, storage, state)
        require(state['legacy_done'] is True and state['native_done'] is True)
        if state['phase'] != 'consolidated':
            for source in sorted(SOURCES):
                await _source_spec(connection, storage, source)
        comparison = await _compare(connection, storage)
        rows = (await connection.execute(text(
            f'SELECT source,count(*) AS records FROM {storage.ledger} GROUP BY source'
        ))).all()
        return validate({
            'source_schema': storage.source_schema, 'native_schema': storage.native_schema,
            'version': state['version'], 'phase': state['phase'],
            'physical_billing_stores': 1 if state['phase'] == 'consolidated' else 3,
            'backfill': {source: bool(state[f'{source}_done']) for source in SOURCES},
            'records': {source: dict(rows).get(source, 0) for source in SOURCES},
            'comparison': comparison,
        }, source_schema=storage.source_schema, native_schema=storage.native_schema)


def execute(options, *, probe_fn, counts_fn, inventory_fn, control_fn, check_locked_fn, snapshot_fn):
    import psycopg
    from app.config import Settings
    from sqlalchemy import text
    from moyai.billing_storage import Storage
    from moyai.billing_storage_cutover import consolidate
    from moyai.postgres import create_engine

    service, command = options['service'], options['command']
    require(command in {'status', 'consolidate'} and service['role'] == 'coordinator')
    state = probe_fn(service)
    require(state.get('ok') is True and state.get('ready') is True and state.get('core') == 'pydantic')
    settings = Settings(_env_file=None)
    storage = Storage(settings.moyai_database_schema, os.environ.get('MOYAI_NATIVE_SCHEMA', ''))
    require(state['schema'] >= 22)
    expected = options.get('expected')
    if command == 'consolidate':
        require(service['staged'] is True and not service['drain']
                and isinstance(expected, dict) and service['sha'] == expected['sha'])
        snapshot_fn(state, expected, expected['schema'])
        require(state.get('source_schema_digest') == expected['source_schema_digest'])
        require(state['native'] == expected['native'] and state['native_drain'] == expected['native_drain'])
        require(expected['native_drain']['paused'] is True)

    async def operation():
        engine = create_engine(settings.moyai_database_url, pool_size=1, max_overflow=0)
        try:
            before = await observe_storage(engine, storage)
            if command == 'status':
                return before

            async def locked(connection):
                # The billing transaction already owns the exclusive canonical
                # fence. Reuse the release's full policy/ownership/drain guard
                # before any table DDL, including both native receipt families.
                pid = await connection.scalar(text('SELECT pg_backend_pid()'))
                with psycopg.connect(settings.moyai_database_url, connect_timeout=10,
                                     autocommit=True) as observer:
                    observer.execute("SET statement_timeout = '5s'")
                    observer.execute("SET lock_timeout = '5s'")
                    check_locked_fn(observer, storage.source_schema, expected,
                                    drain_counts=counts_fn, owner_pid=pid)
                    with observer.transaction():
                        observer.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                        require(inventory_fn(observer) == expected['native'])
                        require(control_fn(observer) == expected['native_drain'])

            comparison = await consolidate(engine, storage, before_consolidate=locked)
            after = await observe_storage(engine, storage)
            # This comparison came from the cutover transaction before the old
            # tables were dropped, not a comparison of the resulting views.
            after['comparison'] = comparison
            return validate(after, source_schema=storage.source_schema,
                            native_schema=storage.native_schema, consolidated=True)
        finally:
            await engine.dispose()

    async def bounded():
        async with asyncio.timeout(120):
            return await operation()

    result = asyncio.run(bounded())
    if command == 'consolidate':
        after = probe_fn(service)
        snapshot_fn(after, expected, expected['schema'])
        require(after['native'] == expected['native'] and after['native_drain'] == expected['native_drain'])
    return result


class SSHBillingStorage:
    def __init__(self, key, *, connections=None):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'billing_migration.py', connections=connections)

    def __call__(self, service, command, expected=None):
        from scripts.release.deploy import ROOT, ReleaseError
        from scripts.release.maintenance_probe import piped_source
        require(command in {'status', 'consolidate'})
        source = (
            "_probe = {'__name__': 'billing_release_probe'}\n"
            'exec(' + repr(piped_source()) + ', _probe)\n'
            "_guard = {'__name__': 'billing_release_guard'}\n"
            'exec(' + repr((ROOT / 'maintenance_migration.py').read_text()) + ', _guard)\n'
            + self.transport.source.read_text())
        value = self.transport.run(service, {
            'service': service.probe_options(), 'command': command, 'expected': expected,
        }, marker='MOYAI_BILLING_MIGRATION=', timeout=180, source=source, read_only=command == 'status')
        try:
            return validate(value, source_schema=service.env['MOYAI_DATABASE_SCHEMA'],
                            native_schema=service.env['MOYAI_NATIVE_SCHEMA'],
                            consolidated=command == 'consolidate')
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ReleaseError(
                'Billing storage is not ready or cutover is unconfirmed. Inspect its backfills, '
                'comparison and release receipt before recovery; no retry was attempted.') from None


if __name__ == '__main__':
    try:
        probe, guard = globals()['_probe'], globals()['_guard']
        value = execute(json.loads(sys.argv[1]), probe_fn=probe['probe'],
            counts_fn=probe['drain_counts'], inventory_fn=probe['_native_probe'].inventory,
            control_fn=probe['_native_probe'].control_receipt,
            check_locked_fn=guard['check_locked'], snapshot_fn=guard['check_snapshot'])
    except Exception:
        value = {'status': 'unconfirmed'}
    print('MOYAI_BILLING_MIGRATION=' + json.dumps(value, sort_keys=True))
