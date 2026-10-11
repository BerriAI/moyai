"""Compute candidate contracts without initializing an application or clients."""
import json
import sys


def contract():
    from render_start import configure_environment
    from app.config import Settings
    from app.runtime_compatibility import api_fingerprint, background_fingerprint
    from app.schema_migrations import SCHEMA_REVISION
    from app.online_migrations import schema_support
    configure_environment()
    settings = Settings(_env_file=None)
    if (settings.moyai_runtime_role not in {'api', 'worker', 'broker', 'coordinator'}
            or settings.moyai_schema_mode != 'verify' or settings.maintenance_drain
            or not settings.moyai_separate_broker or not settings.moyai_coordinator_handoff
            or not settings.temporal_enabled or not settings.object_storage_bucket):
        raise ValueError('Invalid rolling release configuration')
    return {'schema': SCHEMA_REVISION, 'schema_support': schema_support(), 'contracts': {
        'api': api_fingerprint(settings),
        **{role: background_fingerprint(settings, role) for role in ('worker', 'broker', 'coordinator')},
    }}


if __name__ == '__main__':
    try:
        print(json.dumps(contract()))
    except Exception:
        sys.exit(1)  # Settings validation can contain secrets; never print it.
