"""Compute the checked-out candidate's effective API contract without startup.

Called in an isolated process containing only the service environment. Errors
must never include Pydantic input values or configuration secrets.
"""
import json
import sys


def contract():
    from render_start import configure_environment
    from app.config import Settings
    from app.runtime_compatibility import api_fingerprint
    from app.schema_migrations import SCHEMA_REVISION
    configure_environment()
    settings = Settings(_env_file=None)
    if (settings.moyai_runtime_role != 'api' or settings.moyai_schema_mode != 'verify'
            or settings.maintenance_drain or not settings.moyai_separate_broker):
        raise ValueError('Invalid API configuration')
    return {'contract': api_fingerprint(settings), 'schema': SCHEMA_REVISION}


if __name__ == '__main__':
    try:
        print(json.dumps(contract()))
    except Exception:
        sys.exit(1)
