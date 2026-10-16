"""Real local PostgreSQL authority/migration boundaries; supplementary to app proof."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from scenarios import integration_model
from sqlalchemy import String, func, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_product_identity import SCHEMA, google_cookie, request, settings_for

from moyai.contracts import NewMessage, NewSession, PreparedTurn
from moyai.database import Database
from moyai.models import Message, ProductTurnAuthority, Session, Turn, User
from moyai.product_identity import ProductIdentityAdapter
from moyai.product_schema import (
    AUTHORITY_SCHEMA_VERSION,
    GRANT_TABLE,
    GRANT_TRIGGER,
    PRODUCT_SCHEMA_VERSION,
    ProductSchemaError,
    application_metadata,
    migrate_product_schema,
    verify_product_schema,
)
from moyai.runtime import Runtime
from moyai.security import Security
from moyai.settings import Settings
from moyai.settings_runtime import SettingsSkills
from moyai.storage import encrypted_converter
from moyai.stores import NativeStores
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)


@pytest_asyncio.fixture
async def product_database(tmp_path):
    base = urlsplit(os.environ["MOYAI_TEST_POSTGRES"])
    assert base.hostname in {"localhost", "127.0.0.1", "::1"}
    name = "moyai_authority_" + uuid4().hex
    admin = await asyncpg.connect(urlunsplit(base))
    source = database = None
    created = False
    try:
        await admin.execute(f"CREATE DATABASE {name} ENCODING 'UTF8' TEMPLATE template0")
        created = True
        url = urlunsplit(base._replace(path="/" + name))
        source = create_async_engine("postgresql+asyncpg://" + url.split("://", 1)[1])
        # Deliberately long enough to exercise SQLAlchemy's PG index-name hash.
        schema = "moyai_product_authority_regression_execution_schema"
        subject = "long-" + "x" * 240 + "用户" * 10
        user_id = "google:" + subject
        async with source.begin() as connection:
            await connection.execute(text("CREATE SCHEMA moyai"))
            for statement in SCHEMA[:2]:
                await connection.execute(text(statement.replace("CREATE TABLE ", "CREATE TABLE moyai.", 1)))
            await connection.execute(text(
                "INSERT INTO moyai.users VALUES (:id,'google','admin@example.test','Product admin',NULL,'c','u')"
            ), {"id": user_id})
        settings = Settings(
            _env_file=None, data_dir=tmp_path / "never-bootstrap", database_url=url,
            database_schema=schema, product_mode="existing", native_schema_mode="verify",
            encryption_key="synthetic-native-authority-key-no-production", default_model="test",
            workspace_password="must-not-bootstrap-an-admin",
        )
        database = Database(settings, Security(settings.master_key))
        adapter = ProductIdentityAdapter(
            settings_for(tmp_path), source, schema="moyai", native_engine=database.engine,
            allow_shared_identities=True,
        )
        database.product_identity = adapter
        yield SimpleNamespace(
            database=database, adapter=adapter, source=source, schema=schema,
            settings=settings, user_id=user_id, subject=subject,
        )
    finally:
        if database:
            await database.close()
        if source:
            await source.dispose()
        if created:
            await admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
        await admin.close()


@pytest_asyncio.fixture
async def admitted_product(product_database):
    product = product_database
    await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
    await product.database.start()
    cookie, _ = google_cookie(product.adapter, sub=product.subject)
    principal = await product.adapter.authenticate(request(cookie))
    async with product.database.sessions.begin() as db:
        actor = await product.adapter.project_native_user(db, principal)
    product.principal, product.actor = principal, actor
    return product


async def active_turn(product):
    database = product.database
    session = await database.create_session(product.actor, NewSession())
    turn = await database.submit(product.actor, session.id, NewMessage(prompt="Work", request_key=str(uuid4())))
    assert (await database.claim_next()).id == turn.id
    prepared = await database.prepare(turn.id)
    return session, turn, prepared


async def test_projection_reuses_canonical_writer_transaction_when_source_pool_is_exhausted(admitted_product):
    """A contending source writer must not deadlock projection's FK cache update."""
    product = admitted_product
    source = create_async_engine(product.source.url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    original = product.adapter.source_engine
    product.adapter.source_engine = source
    entered = asyncio.get_running_loop().create_future()

    async def competing_writer():
        async with source.begin() as connection:
            entered.set_result(await connection.scalar(text("SELECT pg_backend_pid()")))
            await connection.execute(text(
                "SELECT pg_advisory_xact_lock(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
            ), {"schema": product.adapter.schema})

    competitor = None
    try:
        async with product.database.sessions.begin() as db:
            await db.execute(text(
                "SELECT pg_advisory_xact_lock(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
            ), {"schema": product.adapter.schema})
            competitor = asyncio.create_task(competing_writer())
            pid = await asyncio.wait_for(entered, 2)
            for _ in range(100):
                waiting = await db.scalar(text(
                    "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                ), {"pid": pid})
                if waiting:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("The competing source writer did not reach the real advisory-lock wait.")
            projected = await asyncio.wait_for(product.adapter.project_native_user(db, product.principal), 2)
            assert projected.id == product.user_id
            assert projected.product_grant == product.principal.grant
            assert not competitor.done()
        await asyncio.wait_for(competitor, 2)
    finally:
        product.adapter.source_engine = original
        if competitor is not None:
            await asyncio.gather(competitor, return_exceptions=True)
        await source.dispose()


async def demote(product):
    async with product.source.begin() as db:
        # Same lock protocol as the existing product's role writer.
        await db.execute(text("SELECT pg_advisory_xact_lock(726941, 'moyai'::regnamespace::oid::int)"))
        await db.execute(text(
            "INSERT INTO moyai.user_roles VALUES ('admin@example.test','member',1,'other@example.test','now')"
        ))


async def test_verify_startup_is_read_only_and_never_bootstraps(product_database):
    product = product_database
    with pytest.raises(ProductSchemaError, match="receipt is missing"):
        await product.database.start()
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT to_regnamespace(:schema)"), {"schema": product.schema}) is None
        assert await db.scalar(text("SELECT count(*) FROM moyai.users")) == 1
    assert not product.settings.data_dir.exists()
    assert await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai",
    ) == PRODUCT_SCHEMA_VERSION
    await product.database.start()
    assert not product.settings.data_dir.exists()
    async with product.database.sessions() as db:
        assert await db.scalar(text("SELECT current_schema()")) == product.schema
        assert await db.scalar(select(func.count()).select_from(User)) == 0
    # Reapplying the exact version verifies existing structures.
    await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")


async def test_explicit_width_migration_preserves_existing_rows_and_foreign_keys(product_database):
    product = product_database
    old = application_metadata(product.schema, version=AUTHORITY_SCHEMA_VERSION)
    old.remove(old.tables[f"{product.schema}.{GRANT_TABLE}"])
    for table in old.tables.values():
        for column in table.columns:
            if isinstance(column.type, String) and column.type.length == 512:
                column.type = String(36)
    async with product.source.begin() as db:
        await db.execute(text(f'CREATE SCHEMA "{product.schema}"'))
        await db.run_sync(old.create_all)
        await db.execute(old.tables[f"{product.schema}.moyai_users"].insert().values(
            id="old-native-id", username="old", display_name="Existing", password="unusable",
        ))
        await db.execute(old.tables[f"{product.schema}.moyai_sessions"].insert().values(
            id="old-session", owner_id="old-native-id", model="test",
        ))
    await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
    await verify_product_schema(product.database.engine, schema=product.schema)
    async with product.database.sessions() as db:
        assert (await db.get(Session, "old-session")).owner_id == "old-native-id"
        assert (await db.get(User, "old-native-id")).display_name == "Existing"
        widths = (await db.execute(text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema=:schema AND "
            "((table_name='moyai_users' AND column_name='id') OR column_name IN "
            "('actor_id','owner_id','author_id','user_id','last_actor_id','scope_key'))"
        ), {"schema": product.schema})).scalars().all()
        assert widths and all(width == 512 for width in widths)


async def test_rendered_long_index_names_verify_but_wrong_columns_do_not(admitted_product):
    product = admitted_product
    metadata = application_metadata(product.schema)
    table = metadata.tables[f"{product.schema}.moyai_inference_usage"]
    index = next(index for index in table.indexes if [c.name for c in index.columns] == ["session_id"])
    rendered = postgresql.dialect().identifier_preparer.format_index(index).strip('"')
    assert len(str(index.name)) > 63 and len(rendered) <= 63
    await verify_product_schema(product.database.engine, schema=product.schema)
    async with product.source.begin() as db:
        await db.execute(text(f'DROP INDEX "{product.schema}"."{rendered}"'))
        await db.execute(text(
            f'CREATE INDEX "{rendered}" ON "{product.schema}"."{table.name}" (actor_id)'
        ))
    with pytest.raises(ProductSchemaError, match="Required native index differs"):
        await verify_product_schema(product.database.engine, schema=product.schema)


async def test_uniqueness_and_immutable_trigger_are_required(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        constraints = await db.run_sync(
            lambda conn: inspect(conn).get_unique_constraints("moyai_users", schema=product.schema)
        )
        name = next(row["name"] for row in constraints if row["column_names"] == ["username"])
        await db.execute(text(f'ALTER TABLE "{product.schema}".moyai_users DROP CONSTRAINT "{name}"'))
    with pytest.raises(ProductSchemaError, match="uniqueness constraint differs"):
        await verify_product_schema(product.database.engine, schema=product.schema)


async def test_grant_survives_worker_restart_and_is_immutable(admitted_product):
    product = admitted_product
    session, turn, prepared = await active_turn(product)
    assert session.owner_id == turn.actor_id == prepared.scope.actor_id == product.user_id
    assert prepared.scope.product_grant == product.principal.grant
    async with product.database.sessions() as db:
        saved = await db.get(ProductTurnAuthority, turn.id)
        assert saved.actor_id == product.user_id
        assert saved.authority_version == product.principal.authority_version
    worker = Database(product.settings, product.database.security)
    worker.product_identity = ProductIdentityAdapter(
        product.adapter.settings, product.source, schema="moyai", native_engine=worker.engine,
    )
    try:
        await worker.start()
        assert not hasattr(await worker.user(product.user_id), "product_grant")
        again = await worker.prepare(turn.id)
        assert again == prepared
        await worker.authorize_scope(again.scope)
        encoded = TypeAdapter(PreparedTurn).dump_json(again)
        assert TypeAdapter(PreparedTurn).validate_json(encoded) == again
    finally:
        await worker.close()
    with pytest.raises(IntegrityError, match="immutable"):
        async with product.database.sessions.begin() as db:
            saved = await db.get(ProductTurnAuthority, turn.id)
            saved.authority_version = "0" * 64
    async with product.source.begin() as db:
        await db.execute(text(
            f'ALTER TABLE "{product.schema}"."{GRANT_TABLE}" DISABLE TRIGGER "{GRANT_TRIGGER}"'
        ))
    with pytest.raises(ProductSchemaError, match="immutable product-authority trigger"):
        await verify_product_schema(product.database.engine, schema=product.schema)


async def test_live_demotion_blocks_activity_retry_prepare_and_grant_replacement(admitted_product):
    product = admitted_product
    _, turn, prepared = await active_turn(product)
    assert await SettingsSkills(product.database).catalog(prepared.scope) == []
    await demote(product)
    # The unchanged native profile must not confer authority.
    assert (await product.database.user(product.user_id)).role == "admin"
    with pytest.raises(HTTPException) as denied:
        await SettingsSkills(product.database).catalog(prepared.scope)
    assert denied.value.status_code == 401
    with pytest.raises(PermissionError, match="revoked"):
        await product.database.prepare(turn.id)
    fresh = await product.adapter.current(product.user_id)
    assert fresh.role == "member"
    with pytest.raises(HTTPException) as denied:
        await product.database.authorize_scope(replace(prepared.scope, product_grant=fresh.grant))
    assert denied.value.status_code == 403


async def test_unbound_old_turn_and_automation_owner_lookup_fail_closed(admitted_product):
    product = admitted_product
    session = await product.database.create_session(product.actor, NewSession())
    mirror = await product.database.user(product.user_id)
    with pytest.raises(HTTPException) as denied:
        await product.database.submit(mirror, session.id, NewMessage(prompt="Scheduled", request_key="old-task"))
    assert denied.value.status_code == 401
    async with product.database.sessions.begin() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0
        old = Turn(
            id=str(uuid4()), session_id=session.id, actor_id=product.user_id, request_key="old",
            request_fingerprint="a" * 64, prompt=product.database.security.seal("Old"), model="test",
            workflow_id="old-history-without-admitted-grant",
        )
        db.add(old)
    assert (await product.database.claim_next()).id == old.id
    with pytest.raises(PermissionError, match="no admitted product authority"):
        await product.database.prepare(old.id)
    async with product.database.sessions() as db:
        assert await db.get(ProductTurnAuthority, old.id) is None


async def test_private_session_denied_for_shared_principal(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('shared:password:admin','shared','','Shared',NULL,'c','u')"
        ))
    principal = await product.adapter.current("shared:password:admin")
    async with product.database.sessions.begin() as db:
        actor = await product.adapter.project_native_user(db, principal)
    with pytest.raises(HTTPException) as denied:
        await product.database.create_session(actor, NewSession())
    assert denied.value.status_code == 403
    shared = await product.database.create_session(actor, NewSession(visibility="shared"))
    assert shared.owner_id == principal.id and shared.visibility == "shared"


@pytest.mark.parametrize("revoked_before_save", [True, False])
async def test_result_evidence_survives_revocation_without_new_publication(admitted_product, revoked_before_save):
    product = admitted_product
    session, turn, prepared = await active_turn(product)
    result = {"conversation": "{}", "output": "Confirmed answer", "workspace": None, "usage": {}}
    if revoked_before_save:
        await demote(product)
    accepted = await product.database.save_result(prepared.scope, **result)
    assert accepted is not revoked_before_save
    if not revoked_before_save:
        await demote(product)
    assert await product.database.save_result(prepared.scope, **result) == accepted
    async with product.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert product.database.security.unseal(saved.result).decode() == "Confirmed answer"
        assert saved.result_accepted == accepted
        published = await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == session.id, Message.role == "assistant",
        ))
        assert published == int(accepted)


@pytest.mark.parametrize("revoke", [False, True])
async def test_approval_inherits_original_grant_and_fresh_login_cannot_restore_it(admitted_product, revoke):
    product = admitted_product
    session, turn, prepared = await active_turn(product)
    assert await product.database.save_result(
        prepared.scope, conversation="{}", output="Approval required",
        workspace=None, usage={}, deferred=True,
    )
    actor = product.actor
    if revoke:
        await demote(product)
        principal = await product.adapter.current(product.user_id)
        async with product.database.sessions.begin() as db:
            actor = await product.adapter.project_native_user(db, principal)
        with pytest.raises(HTTPException) as denied:
            await product.database.resume(actor, session.id, turn.id, "approval", {})
        assert denied.value.status_code == 403
    else:
        resumed = await product.database.resume(actor, session.id, turn.id, "approval", {})
        async with product.database.sessions() as db:
            grant = await db.get(ProductTurnAuthority, resumed.id)
            assert grant.authority_version == prepared.scope.product_grant.authority_version


async def test_submit_waiting_on_canonical_writer_rechecks_before_creating_work(admitted_product):
    product = admitted_product
    session = await product.database.create_session(product.actor, NewSession())
    pending = None
    try:
        async with product.source.begin() as writer:
            await writer.execute(text(
                "SELECT pg_advisory_xact_lock(726941, 'moyai'::regnamespace::oid::int)"
            ))
            pending = asyncio.create_task(product.database.submit(
                product.actor, session.id, NewMessage(prompt="Must be denied", request_key="race"),
            ))
            done, _ = await asyncio.wait({pending}, timeout=0.1)
            assert not done
            await writer.execute(text(
                "INSERT INTO moyai.user_roles VALUES ('admin@example.test','member',1,'other@example.test','now')"
            ))
        with pytest.raises(HTTPException) as denied:
            await asyncio.wait_for(pending, 5)
        assert denied.value.status_code == 401
        async with product.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(Turn)) == 0
            assert await db.scalar(select(func.count()).select_from(ProductTurnAuthority)) == 0
    finally:
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_product_grant_and_canonical_memory_run_in_real_temporal_and_replay(admitted_product, tmp_path):
    """Controlled model with real PG stores/worker; not provider or actual-app proof."""
    product = admitted_product
    # Inject the adapter at its documented assembly boundary. This fixture only
    # seeds canonical identity tables, not the entire historical product schema.
    settings = product.settings.model_copy(update={"product_mode": "standalone"})
    provision = NativeStores(settings)
    try:
        await provision.start(provision=True)
    finally:
        await provision.close()
    runtime = Runtime(settings)
    runtime.product_identity = ProductIdentityAdapter(
        product.adapter.settings, product.source, schema="moyai", native_engine=runtime.database.engine,
    )
    runtime.database.product_identity = runtime.product_identity
    workspace = tmp_path / "computer"
    workspace.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "skills", workspace / "skills")
    try:
        await runtime.start(
            models={"test": integration_model("tools")}, workspace=LocalWorkspace(workspace),
            skills_directory="skills",
        )
        session, turn, prepared = await active_turn(product)
        converter = encrypted_converter(runtime.security, runtime.objects)
        environment = await WorkflowEnvironment.start_local(
            plugins=[PydanticAIPlugin()], data_converter=converter,
        )
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                execution = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=settings.temporal_queue,
                )
                result = await asyncio.wait_for(execution.result(), timeout=45)
                assert result["status"] == "completed"
                history = await execution.fetch_history()
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            ).replay_workflow(history)
        memory = await runtime.stores.memory.read(
            prepared.scope.memory_namespace + "/main/MEMORY.md", max_chars=1000,
        )
        assert memory is not None
        assert "native capability proof succeeded" in memory.content
        assert (workspace / "proof.txt").is_file()
        assert len(await runtime.stores.plan(session.id).get_items()) == 1
        children = await runtime.stores.steps.list_runs(parent_run_id=turn.id)
        assert len(children) == 1 and ":" in children[0].run_id
        assert children[0].parent_run_id == turn.id
        async with product.database.sessions() as db:
            saved = await db.get(Turn, turn.id)
            assert saved.result_accepted is True
            assert (await db.get(ProductTurnAuthority, turn.id)).authority_version == product.principal.authority_version
    finally:
        await runtime.close()
