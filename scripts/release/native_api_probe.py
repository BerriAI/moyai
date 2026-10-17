"""Read-only native API admission while background execution remains running.

Ship the observation helpers to the installed application. Native transaction
fences remain visible to ownership validation; none are mistaken for API owners.
No runtime, schema migration, maintenance request or external client is started.
"""
import os

if '_rolling_probe' not in globals():
    from scripts.release import rolling_probe as _rolling_probe
if '_native_probe' not in globals():
    from scripts.release import native_probe as _native_probe

require = _rolling_probe.require


def snapshot(connection, schema):
    from app.schema_migrations import SCHEMA_REVISION, prepare_schema

    state = _rolling_probe.snapshot(connection, schema)
    # This first milestone admits no canonical or native schema migration.
    require(state['schema'] == SCHEMA_REVISION)
    prepare_schema(connection, schema, 'verify')
    native = _native_probe.inventory(connection)
    drain = _native_probe.control_receipt(connection)
    require(isinstance(native, dict) and native.get('state') == 'receipted')
    require(isinstance(drain, dict) and drain.get('core') == 'pydantic-v1'
            and drain.get('native_schema') == native['schema']
            and drain.get('task_queue') == os.environ.get('MOYAI_NATIVE_TASK_QUEUE')
            and type(drain.get('generation')) is int and drain['generation'] > 0
            and drain.get('paused') is False)
    return {**state, 'contract': state['contracts']['api'],
            'native': native, 'native_drain': drain}


def health_identity(response, options, state):
    require(response.headers.get('X-Moyai-Core') == 'pydantic-v1')
    if options['role'] == 'api':
        require(response.headers.get('X-Moyai-API-Release') == '1')
    health = _rolling_probe.health_identity(response, options, state)
    return {**health, 'core': 'pydantic-v1', 'role': options['role'],
            'api_owner': health['identity'] if options['role'] == 'api' else None}


def require_configuration():
    require(os.environ.get('MOYAI_CORE') == 'pydantic'
            and os.environ.get('MOYAI_PRODUCT_MODE') == 'existing'
            and os.environ.get('MOYAI_NATIVE_SCHEMA_MODE', 'verify') == 'verify'
            and os.environ.get('MOYAI_PRODUCT_LOCAL_REHEARSAL', 'false') == 'false')


def probe(options):
    require_configuration()
    return _rolling_probe.probe(options, snapshot_fn=snapshot, health_fn=health_identity)


def piped_source():
    from scripts.release.deploy import ROOT

    # Render images need not contain scripts/. Only observation helpers come
    # from the candidate; contracts and receipts come from the installed app.
    source = 'import types\n'
    for name, filename in (('_rolling_probe', 'rolling_probe.py'), ('_native_probe', 'native_probe.py')):
        source += f'{name} = types.ModuleType({name!r})\n'
        source += f'exec({(ROOT / filename).read_text()!r}, {name}.__dict__)\n'
    source += "_native_api_probe = types.ModuleType('moyai_native_api_probe')\n"
    source += '_native_api_probe.__dict__.update(_rolling_probe=_rolling_probe, _native_probe=_native_probe)\n'
    source += f'exec({(ROOT / "native_api_probe.py").read_text()!r}, _native_api_probe.__dict__)\n'
    return source + (ROOT / 'api_probe.py').read_text()


class SSHAPIProbe:
    def __init__(self, key, *, connections=None):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'api_probe.py', connections=connections)

    def invalidate(self, service):
        self.transport.invalidate(service)

    def __call__(self, service):
        return self.transport.run(service, service.probe_options(), marker='MOYAI_RELEASE_PROBE=',
                                  source=piped_source(), read_only=True)
