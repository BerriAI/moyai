"""Observe native worker membership alongside canonical rolling ownership."""
import json
import os
import sys

if '_native_api_probe' not in globals():
    from scripts.release import native_api_probe as _native_api_probe

_rolling_probe = _native_api_probe._rolling_probe
require = _rolling_probe.require


def workers(connection, state):
    from psycopg import sql

    rows = connection.execute(sql.SQL("""
        SELECT w.owner_pid,a.backend_start,w.requested_drain,w.source_policy,w.worker_policy
        FROM {}.moyai_product_drain_workers w
        JOIN pg_stat_activity a ON a.pid=w.owner_pid
            AND a.backend_start=CAST(w.owner_started AS timestamptz)
        ORDER BY w.owner_pid,a.backend_start
    """).format(sql.Identifier(os.environ['MOYAI_NATIVE_SCHEMA']))).fetchall()
    observed = []
    for pid, started, drain, policy, contract in rows:
        identity = f'{pid}@{started.isoformat()}'
        require(identity in state['owners']
                and identity not in state['coordinators'] + state['brokers']
                and drain is False and policy == state['policy']
                and contract == state['contracts']['worker'])
        observed.append(identity)
    require(len(observed) == len(set(observed)))
    return sorted(observed)


def snapshot(connection, schema):
    # One read-only repeatable-read transaction contains schemas, contracts,
    # process ownership, maintenance receipt and native worker registrations.
    state = _native_api_probe.snapshot(connection, schema)
    state['native_workers'] = workers(connection, state)
    return state


def worker_shutdown_seconds():
    """Read the installed application's effective settings without starting it."""
    from moyai.product_config import load_product_config
    from moyai.settings import Settings

    config = load_product_config()
    require(config is not None and config.role == 'worker')
    return Settings(_env_file=None, **config.native_overrides()).worker_shutdown_seconds


def probe(options):
    _native_api_probe.require_configuration()
    state = _rolling_probe.probe(
        options, snapshot_fn=snapshot, health_fn=_native_api_probe.health_identity)
    if options['role'] == 'worker':
        # rolling_probe has normalized the installed Render entrypoint's
        # environment, including defaults absent from control-plane metadata.
        state['worker_shutdown_seconds'] = worker_shutdown_seconds()
        if state['ready'] is True:
            require(state['identity'] in state['native_workers'])
    return state


def result(options):
    try:
        return probe(options)
    except _rolling_probe.ProbeFailure as error:
        return {'ok': False, 'reason': error.reason, **({'stage': error.stage} if error.stage else {})}
    except Exception:
        return {'ok': False, 'reason': 'runtime_validation_failed'}


def piped_source():
    from scripts.release.deploy import ROOT

    # Reuse the bundled observation helpers without importing scripts/ from a
    # production image. Source application imports still resolve on that image.
    bootstrap = ("_helpers = {'__name__': 'native_rolling_helpers'}\n"
                 'exec(' + repr(_native_api_probe.piped_source()) + ', _helpers)\n'
                 "_native_api_probe = _helpers['_native_api_probe']\n")
    return bootstrap + (ROOT / 'native_rolling_probe.py').read_text()


class SSHNativeRollingProbe:
    def __init__(self, key, *, connections=None):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'native_rolling_probe.py', connections=connections)

    def invalidate(self, service):
        self.transport.invalidate(service)

    def __call__(self, service):
        return self.transport.run(service, service.probe_options(), marker='MOYAI_RELEASE_PROBE=',
                                  source=piped_source(), read_only=True)


if __name__ == '__main__':
    try:
        value = result(json.loads(sys.argv[1]))
    except Exception:
        value = {'ok': False, 'reason': 'probe_request_invalid'}
    print('MOYAI_RELEASE_PROBE=' + json.dumps(value, sort_keys=True))
