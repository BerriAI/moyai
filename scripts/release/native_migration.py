"""Native provisioning inside the existing maintenance zero-owner boundary.

The outer transport stays in legacy Python. Only the locked native interpreter
imports the native schema/dependency modules. No transport write is retried.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

def native_python(root=Path('/app')):
    previous = root / 'native/.venv/bin/python'
    return str(previous if previous.is_file() else root / '.venv/bin/python')

SCHEMA_IDENTIFIERS_SOURCE = '''
def validate_schema_identifiers(metadata):
    from sqlalchemy.dialects import postgresql
    preparer = postgresql.dialect().identifier_preparer
    names = {table.name for table in metadata.tables.values()}
    for table in metadata.sorted_tables:
        for index in table.indexes:
            name = preparer.format_index(index).strip('"')
            if name in names:
                raise ValueError("Native application index names collide after PostgreSQL shortening.")
            names.add(name)
'''
MANIFEST_SOURCE = '''
import json
from moyai.product_config import load_product_config
from moyai.product_deployment import check_runtime_dependencies, require_startable
from moyai.product_schema import MIGRATIONS, application_metadata, _manifest
config = load_product_config()
assert config and config.product_mode == "existing" and not config.local_rehearsal
require_startable(config)
versions = check_runtime_dependencies()
''' + SCHEMA_IDENTIFIERS_SOURCE + '''
try:
    validate_schema_identifiers(application_metadata(config.native_schema))
except ValueError:
    print(json.dumps({"error": "native_schema_identifier_collision"}))
    raise SystemExit(1)
print(json.dumps({"schema": config.native_schema,
    "app": {v: _manifest(application_metadata(config.native_schema, version=v)) for v, _ in MIGRATIONS},
    "harness": {"sdk_version": versions["pydantic-ai"], "harness_version": versions["pydantic-ai-harness"]}}))
'''


def candidate_manifest(environment):
    """Pure candidate checks: no DB, SDK store, agent or provider startup."""
    from scripts.release.deploy import ReleaseError
    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run([native_python(root), '-c', MANIFEST_SOURCE],
            cwd=root, env={**environment, 'PATH': os.defpath, 'PYTHONPATH': str(root)},
            capture_output=True, text=True, timeout=60)
        value = json.loads(result.stdout)
        if value == {'error': 'native_schema_identifier_collision'}:
            raise ReleaseError(
                'The selected native schema causes PostgreSQL index-name collisions. '
                'A verified-empty native schema can use a different explicit name; '
                'a populated schema needs a reviewed index migration. No changes made.')
        if result.returncode or not isinstance(value, dict) or set(value) != {'schema', 'app', 'harness'}:
            raise ValueError()
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ReleaseError('Native candidate configuration, qualified roles, locked dependencies or migration manifests could not be verified; no changes made.') from None


def native_command(command):
    """Bounded, captured output; errors never echo settings or SQL diagnostics."""
    result = subprocess.run([native_python(), '-m', 'moyai.product_start', *command],
        cwd='/app', capture_output=True, text=True, timeout=480)
    if result.returncode:
        raise ValueError('Native schema command failed.')
    value = json.loads(result.stdout)
    if (not isinstance(value, dict) or value.get('core') != 'pydantic-v1'
            or value.get('native_schema') != os.environ.get('MOYAI_NATIVE_SCHEMA')):
        raise ValueError('Native schema command returned an invalid receipt.')
    if command == ['verify']:
        if value.get('app_schema') != 'verified' or value.get('harness_schema') != 'verified':
            raise ValueError('Native verification did not confirm both receipts.')
    elif (value.get('harness_schema') != 'provisioned'
          or not isinstance(value.get('app_schema'), str) or not value['app_schema']):
        raise ValueError('Native provisioning did not confirm both receipts.')
    return value


def execute(options, *, probe_fn, counts_fn, inventory_fn, plan_fn, check_locked_fn):
    import psycopg
    from app.config import Settings

    service, expected, plan = options['service'], options['expected'], options['plan']
    from_check = globals().get('_check_snapshot')
    if from_check is None:
        from scripts.release.maintenance_migration import check_snapshot as from_check
    if (service['role'] != 'coordinator' or not service['staged'] or service['drain']
            or service['sha'] != expected['sha'] or os.environ.get('MOYAI_CORE') != 'pydantic'):
        raise ValueError('Native provisioning requires the staged candidate coordinator.')
    from_check(probe_fn(service), expected, expected['schema'])
    settings = Settings(_env_file=None)
    schema = settings.moyai_database_schema
    with psycopg.connect(settings.moyai_database_url, connect_timeout=10, autocommit=True) as connection:
        # Session lock spans BOTH native app and SDK transactions/processes.
        locked = connection.execute(
            'SELECT pg_try_advisory_lock(726940,%s::regnamespace::oid::int)', (schema,)
        ).fetchone()[0]
        if not locked:
            raise ValueError('A source owner is still active.')
        check_locked_fn(connection, schema, expected, drain_counts=counts_fn)
        with connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            if plan_fn(inventory_fn(connection), plan['target']) != plan:
                raise ValueError('Native migration receipt changed after preflight.')
        if plan['apply']:
            native_command(['provision', '--apply'])
        native_command(['verify'])
        with connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            after = plan_fn(inventory_fn(connection), plan['target'])
            if after['apply']:
                raise ValueError('Native migration receipts did not reach the target.')
        check_locked_fn(connection, schema, expected, drain_counts=counts_fn)
    from_check(probe_fn(service), expected, expected['schema'])
    return {'status': 'verified', 'target': plan['target']}


class SSHNativeMigration:
    def __init__(self, key):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'native_migration.py')

    def __call__(self, service, plan, expected):
        from scripts.release.deploy import ROOT, ReleaseError
        from scripts.release.maintenance_probe import piped_source
        source = (
            "_probe = {'__name__': 'native_release_probe'}\n"
            'exec(' + repr(piped_source()) + ', _probe)\n'
            "_guard = {'__name__': 'native_release_guard'}\n"
            'exec(' + repr((ROOT / 'maintenance_migration.py').read_text()) + ', _guard)\n'
            "_check_snapshot = _guard['check_snapshot']\n" + self.transport.source.read_text())
        result = self.transport.run(service, {'service': service.probe_options(),
            'plan': plan, 'expected': expected}, marker='MOYAI_NATIVE_MIGRATION=', timeout=600, source=source)
        if result != {'status': 'verified', 'target': plan['target']}:
            raise ReleaseError('Native migration failed or its outcome is uncertain. All roles remain staged; inspect both migration receipts before recovery.')
        return result


if __name__ == '__main__':
    try:
        value = execute(json.loads(sys.argv[1]), probe_fn=_probe['probe'],
            counts_fn=_probe['drain_counts'], inventory_fn=_probe['_native_probe'].inventory,
            plan_fn=_probe['_native_probe'].migration_plan, check_locked_fn=_guard['check_locked'])
    except Exception:
        value = {'status': 'unconfirmed'}
    print('MOYAI_NATIVE_MIGRATION=' + json.dumps(value, sort_keys=True))
