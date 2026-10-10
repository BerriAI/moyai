"""Export contracts offline, with isolated local settings and no server startup."""
import json
import os
from pathlib import Path
import sys
import tempfile
import warnings

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

with tempfile.TemporaryDirectory(prefix='moyai-openapi-') as directory:
    # app.main creates the default app on import. Isolate that construction too.
    os.environ.update(DATA_DIR=directory, TEMPORAL_ENABLED='false',
                      MOYAI_DATABASE_URL='', MOYAI_RUNTIME_ROLE='standalone',
                      LITELLM_API_BASE='', LITELLM_API_KEY='',
                      SESSION_TITLES_ENABLED='false', MEMORY_REVIEW_ENABLED='false',
                      SANDBOX_PREPARED_POOL_SIZE='0')
    from app.main import app

    # Export only the contracts migrated to the typed client. Untyped response
    # endpoints must not quietly appear as a usable generated client.
    selected = {'/api/session', '/api/settings/preferences', '/api/settings/session-titles',
                '/api/memory', '/api/memory/preferences', '/api/memory/{note_id}'}
    # FastAPI also expands included routers here; filtering app.routes directly
    # would omit those endpoints on newer FastAPI versions.
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Duplicate Operation ID')
        schema = app.openapi()
    schema['paths'] = {key: value for key, value in schema['paths'].items() if key in selected}
    needed = set()

    def references(value):
        if isinstance(value, dict):
            if '$ref' in value:
                name = value['$ref'].rsplit('/', 1)[-1]
                if name not in needed:
                    needed.add(name)
                    references(schema['components']['schemas'][name])
            for child in value.values():
                references(child)
        elif isinstance(value, list):
            for child in value:
                references(child)

    references(schema['paths'])
    schema['components']['schemas'] = {key: value for key, value in schema['components']['schemas'].items() if key in needed}
    destination = root / 'frontend/api/openapi.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(schema, indent=2, sort_keys=True) + '\n')
