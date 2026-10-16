"""The installed Pydantic interpreter can prepare and preserve the product database."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest


@pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Requires disposable PostgreSQL.")
def test_canonical_migration_never_loads_retired_execution_engines(tmp_path):
    url = os.environ["MOYAI_TEST_POSTGRES"]
    schema = "moyai_test_" + uuid4().hex
    program = r'''
import importlib.abc
import json
import os
from pathlib import Path
import sys

class NoEngines(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ("app.main", "app.runner", "app.durable_runner", "app.deepagents_runtime",
                   "agents", "deepagents", "langchain", "temporalio_deepagents")
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise ImportError("Migration attempted to load retired engine: " + fullname)

sys.meta_path.insert(0, NoEngines())
from cryptography.fernet import Fernet
from app.config import Settings
from app.db import Store
from app.schema_migrations import SCHEMA_REVISION, migrate

settings = Settings(
    _env_file=None, data_dir=Path(os.environ["TEST_DATA"]),
    moyai_database_url=os.environ["TEST_POSTGRES"], moyai_database_schema=os.environ["TEST_SCHEMA"],
    moyai_database_initialize=True,
    encryption_key=Fernet.generate_key().decode(), session_secret="isolated-schema-test",
    agent_model="openai/gpt-6-astra",
)
assert migrate(settings) == {"schema_revision": SCHEMA_REVISION, "status": "ready"}
def open_store():
    return Store(settings.data_dir, default_model=settings.agent_model,
                 database_url=settings.moyai_database_url, database_schema=settings.moyai_database_schema,
                 schema_mode="verify")
store = open_store()
try:
    saved = store.create_run("Preserve this historical session", "", "modal", [])
finally:
    store.close()
assert migrate(settings)["schema_revision"] == SCHEMA_REVISION
store = open_store()
try:
    assert store.run(saved["id"])["prompt"] == "Preserve this historical session"
finally:
    store.close()
print(json.dumps({"revision": SCHEMA_REVISION, "preserved": True}))
'''
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{schema}"')
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "TEST_POSTGRES": url, "TEST_SCHEMA": schema, "TEST_DATA": str(tmp_path)},
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["preserved"] is True
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_schema_initializers_have_a_versioned_manifest():
    # A schema edit without a new revision must fail CI, including optional
    # components and backend-specific functions/triggers used by the migrator.
    import ast
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    from app import schema_migrations as migration
    from app.schema_initializers import TABLES
    sources = {}
    for path in (root / 'app').glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if path.name == 'schema_initializers.py' and isinstance(node, ast.FunctionDef) and node.name.startswith('initialize_'):
                component = node.name.removeprefix('initialize_')
                node.name = 'initialize_schema'
                sources[f'{component}.py:initialize_schema'] = ast.dump(node, include_attributes=False)
                continue
            if isinstance(node, ast.FunctionDef) and node.name in {'initialize_schema', '_initialize_schema', 'initialize_defaults'}:
                sources[f'{path.name}:{node.name}'] = ast.dump(node, include_attributes=False)
    sources['database_schema'] = ast.dump(ast.parse((root / 'app/database_schema.py').read_text()), include_attributes=False)
    sources['components'] = list(migration.COMPONENTS)
    sources['trace_tables'] = sorted(TABLES)
    assert {k.split(':')[0][:-3] for k in sources if k.endswith(':initialize_schema')} == (
        set(migration.COMPONENTS) | {'attachments', 'slack_mentions', 'blob_storage', 'trace_outbox'})
    signature = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    versions = json.loads((root / 'app/schema_versions.json').read_text())
    assert list(versions) == [str(i) for i in range(1, migration.SCHEMA_REVISION + 1)]
    assert versions[str(migration.SCHEMA_REVISION)] == signature, (
        'Schema changed. Increment SCHEMA_REVISION and append its source digest to app/schema_versions.json: ' + signature)
