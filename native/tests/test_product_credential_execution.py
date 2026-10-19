"""Real local SQL request/resume boundaries; supplementary to real model/Modal proof."""

import asyncio
import base64
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_authority as authority
from app.credentials import CredentialRequest, ListCredentials, Materialize, Resolve
from fastapi import HTTPException
from pydantic import SecretStr
from pydantic_ai import Agent, Conversation, ConversationTypeAdapter, DeferredToolRequests
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from moyai.effects import EffectReceipts, confirm_effect
from moyai.models import Session, Turn
from moyai.product_continuation_models import ProductContinuation
from moyai.product_credential_models import ProductCredentialRequest as AccessRequest
from moyai.product_credential_tools import capability, redact_output
from moyai.product_secrets import ProductSecrets

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
product_database = authority.product_database
admitted_product = authority.admitted_product


@pytest_asyncio.fixture
async def credentials(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        await db.execute(text("""CREATE TABLE moyai.provider_secrets(
            id TEXT PRIMARY KEY,provider TEXT NOT NULL,label TEXT NOT NULL,scope TEXT NOT NULL,
            owner_id TEXT NOT NULL,root_id TEXT NOT NULL DEFAULT '',encrypted TEXT NOT NULL,
            created_at TEXT NOT NULL,revoked_at TEXT NOT NULL DEFAULT '',client_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',format TEXT NOT NULL DEFAULT 'env',env_var TEXT NOT NULL DEFAULT '',
            lifetime TEXT NOT NULL DEFAULT 'persistent',expires_at TEXT NOT NULL DEFAULT '',
            invalid_reason TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,
            UNIQUE(owner_id,client_id))"""))
        await db.execute(text("""CREATE TABLE moyai.credential_audit(
            id BIGSERIAL PRIMARY KEY,actor_id TEXT NOT NULL,secret_id TEXT NOT NULL,
            action TEXT NOT NULL,run_id TEXT NOT NULL,created_at TEXT NOT NULL)"""))
    runtime = SimpleNamespace(
        database=product.database, security=product.database.security, settings=product.settings,
        product=SimpleNamespace(
            identity=product.adapter, security=product.adapter.legacy_security,
            settings=product.adapter.settings, store_proxy=SimpleNamespace(schema_updates=False),
        ),
    )
    vault = ProductSecrets(runtime)
    session, turn, prepared = await authority.active_turn(product, private=True)
    return SimpleNamespace(product=product, service=vault.execution, vault=vault, scope=prepared.scope,
                           session=session, turn=turn, runtime=runtime)


def request_body(**overrides):
    return CredentialRequest(
        provider="generic", name="webhook-signing-secret", reason="Configure the authorized webhook receiver.",
        request_key="webhook-access", input_fields=[{"name": "WEBHOOK_SECRET", "label": "Signing secret"}],
        **overrides,
    )


def test_materialize_request_ids_explain_saved_credential_handle_flow():
    assert Materialize.model_fields["request_ids"].description == (
        "IDs returned by credentials_request. To use saved access, first call credentials_request with "
        "secret_id set to the credential id from credentials_list; credential ids are not request IDs."
    )


async def wait_for_credential(fixture, args=None, *, call_id="credential-call"):
    args = args or request_body()
    scope, service = fixture.scope, fixture.service
    result = await service.request(scope, scope.turn_id, call_id, args)
    call = ToolCallPart("credentials_request", {"request": args.model_dump(mode="json")}, tool_call_id=call_id)
    conversation = Conversation(
        messages=[ModelResponse(parts=[call])],
        deferred_tool_requests=DeferredToolRequests(calls=[call], metadata={call_id: result}),
    )
    await fixture.product.database.save_result(
        scope, conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        output="", workspace=None, usage={}, deferred=True,
    )
    return result


def provide(**changes):
    return Resolve(
        generation=0, scope="personal", lifetime="persistent",
        value=SecretStr('{"WEBHOOK_SECRET":"synthetic-signing-material-123456"}'),
        **changes,
    )


async def test_native_request_resolves_once_and_resumes_with_only_handle(credentials):
    f = credentials
    result = await wait_for_credential(f)
    body = provide().model_copy(update={"scope": "organization"})
    response = await f.service.resolve(None, f.product.principal, result["request_id"], body)
    assert response["resumed"]
    duplicate = await f.service.resolve(None, f.product.principal, result["request_id"], body)
    assert duplicate["turn_id"] == response["turn_id"]
    async with f.product.database.sessions() as db:
        row = await db.get(AccessRequest, result["request_id"])
        turn = await db.get(Turn, response["turn_id"])
        assert row.status == "provided" and row.secret_revision == 1
        assert "synthetic-signing-material" not in row.capability
        deferred = f.runtime.security.unseal(turn.deferred_results).decode()
        assert "synthetic-signing-material" not in deferred and result["request_id"] in deferred
        assert await db.scalar(select(func.count()).select_from(Turn)) == 2
    async with f.product.source.connect() as db:
        secret = (await db.execute(text("SELECT * FROM moyai.provider_secrets"))).mappings().one()
        assert "synthetic-signing-material" not in secret["encrypted"]
    assert (await f.product.database.claim_next()).id == response["turn_id"]
    resumed = await f.product.database.prepare(response["turn_id"])
    async with f.product.database.sessions.begin() as db:
        value = await f.service.webhook_secret(resumed.scope, resumed.scope.turn_id, result["request_id"], db=db)
        assert value.get_secret_value() == "synthetic-signing-material-123456"
        assert "synthetic-signing-material" not in repr(value)


@pytest.mark.parametrize("private_parent", [False, True])
async def test_private_child_does_not_expand_personal_credential_visibility(credentials, private_parent):
    from moyai.delegation import DelegationService

    f = credentials
    _, _, prepared = await authority.active_turn(f.product, private=private_parent)
    child = await DelegationService(f.product.database).start(prepared.scope, "child", "Bounded work")
    secret = {"lifetime": "persistent", "scope": "personal", "owner_id": f.product.actor.id}
    async with f.product.database.sessions() as db:
        assert await f.service.visible(db, secret, f.product.actor.id, child["session_id"]) is private_parent
        assert await f.service.visible(
            db, {**secret, "scope": "organization"}, f.product.actor.id, child["session_id"],
        )


async def test_resolution_rejects_stale_generation_changed_input_and_stopped_turn(credentials):
    f = credentials
    pending = await wait_for_credential(f)
    stale = provide().model_copy(update={"generation": 1})
    with pytest.raises(HTTPException):
        await f.service.resolve(None, f.product.principal, pending["request_id"], stale)
    await f.product.database.stop(f.product.actor, f.session.id)
    with pytest.raises(HTTPException):
        await f.service.resolve(None, f.product.principal, pending["request_id"], provide())
    async with f.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.provider_secrets")) == 0


async def test_saved_secret_reuse_rechecks_revision_scope_and_no_values_enter_inventory(credentials):
    f = credentials
    pending = await wait_for_credential(f)
    response = await f.service.resolve(None, f.product.principal, pending["request_id"], provide())
    await f.product.database.claim_next()
    prepared = await f.product.database.prepare(response["turn_id"])
    inventory = await f.service.inventory(prepared.scope, ListCredentials())
    assert len(inventory["credentials"]) == 1
    assert "synthetic-signing-material" not in json.dumps(inventory)
    reused = await f.service.request(prepared.scope, prepared.scope.turn_id, "new-call", request_body())
    assert reused["status"] == "provided"
    async with f.product.source.begin() as db:
        await db.execute(text("UPDATE moyai.provider_secrets SET revision=revision+1"))
    with pytest.raises(HTTPException):
        await f.service.materialize(prepared.scope, [reused["request_id"]])
    with pytest.raises(HTTPException):
        await f.service.materialize(replace(prepared.scope, actor_id="someone-else"), [reused["request_id"]])


async def test_request_handle_identity_does_not_rebind_or_leak_to_delegates(credentials):
    f = credentials
    first = await f.service.request(f.scope, f.scope.turn_id, "one", request_body())
    assert await f.service.request(f.scope, f.scope.turn_id, "one", request_body()) == first
    with pytest.raises(HTTPException):
        await f.service.request(f.scope, f.scope.turn_id, "two", request_body())
    with pytest.raises(HTTPException):
        await f.service.request(f.scope, "child-agent", "two", request_body())
    with pytest.raises(HTTPException):
        await f.service.request(f.scope, f.scope.turn_id, "one", request_body().model_copy(update={"reason": "Different intent"}))


@pytest.mark.parametrize("changed", ["flat_content", "extra_wrapper_field"])
async def test_call_normalization_does_not_accept_changed_or_ambiguous_arguments(credentials, changed):
    f = credentials
    pending = await wait_for_credential(f)
    async with f.product.database.sessions.begin() as db:
        turn = await db.get(Turn, f.turn.id)
        conversation = ConversationTypeAdapter.validate_json(f.runtime.security.unseal(turn.conversation))
        arguments = request_body().model_dump(mode="json")
        conversation.deferred_tool_requests.calls[0].args = (
            {**arguments, "reason": "Changed after request admission."}
            if changed == "flat_content" else {"request": arguments, "provider": "generic"}
        )
        turn.conversation = f.runtime.security.seal(ConversationTypeAdapter.dump_json(conversation).decode())
    with pytest.raises(HTTPException, match="request changed"):
        await f.service.resolve(None, f.product.principal, pending["request_id"], provide())
    async with f.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.provider_secrets")) == 0


async def test_original_authority_revocation_prevents_resolution(credentials):
    f = credentials
    pending = await wait_for_credential(f)
    await authority.demote(f.product)
    with pytest.raises(HTTPException):
        await f.service.resolve(None, f.product.principal, pending["request_id"], provide())
    async with f.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.provider_secrets")) == 0


async def test_shared_and_slack_sessions_cannot_use_personal_agent_credentials(credentials):
    f = credentials
    pending = await wait_for_credential(f)
    response = await f.service.resolve(None, f.product.principal, pending["request_id"], provide())
    await f.product.database.claim_next()
    prepared = await f.product.database.prepare(response["turn_id"])
    async with f.product.database.sessions.begin() as db:
        with pytest.raises(HTTPException, match="Shared webhook"):
            await f.service.webhook_secret(prepared.scope, prepared.scope.turn_id, pending["request_id"], db=db)
    for surface in ["web", "slack"]:
        async with f.product.database.sessions.begin() as db:
            session = await db.get(Session, prepared.scope.session_id)
            session.visibility, session.surface = "shared", surface
        shared = replace(prepared.scope, visibility="shared", surface=surface)
        if surface == "slack":
            from test_product_automations import bind_slack_test_session
            await bind_slack_test_session(f.product.database, session.id, f.product.actor)
        assert not (await f.service.inventory(shared, ListCredentials()))["credentials"]
        with pytest.raises(HTTPException):
            await f.service.materialize(shared, [pending["request_id"]])


async def test_decline_resumes_without_storing_credentials(credentials):
    f = credentials
    pending = await wait_for_credential(f)
    response = await f.service.resolve(None, f.product.principal, pending["request_id"], Resolve(decision="decline", generation=0))
    assert response["resumed"]
    async with f.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.provider_secrets")) == 0


@pytest.mark.parametrize("wrapped", [False, True])
async def test_real_sdk_defers_native_request_with_server_handle_and_no_secret(credentials, wrapped):
    f = credentials
    arguments = request_body().model_dump(mode="json")
    model = FunctionModel(lambda _messages, _info: ModelResponse(parts=[
        ToolCallPart("credentials_request", {"request": arguments} if wrapped else arguments,
                     tool_call_id="sdk-credential-call"),
    ]))
    agent = Agent(
        model, deps_type=type(f.scope), output_type=[str, DeferredToolRequests],
        capabilities=[capability(f.service)],
    )
    result = await agent.run("Obtain the authorized access.", deps=f.scope, run_id=f.scope.turn_id)
    assert isinstance(result.output, DeferredToolRequests)
    assert result.output.calls[0].tool_name == "credentials_request"
    pending = result.output.metadata["sdk-credential-call"]
    assert pending["kind"] == "credential_request"
    assert pending["status"] == "pending" and len(pending["request_id"]) == 32
    assert "value" not in pending
    async with f.product.database.sessions() as db:
        assert (await db.get(AccessRequest, pending["request_id"])).tool_call_id == "sdk-credential-call"
    # Persist the SDK's actual conversation, not a reconstructed wrapped call.
    # Real providers follow its flattened single-Pydantic-argument schema.
    await f.product.database.save_result(
        f.scope, conversation=ConversationTypeAdapter.dump_json(result.conversation).decode(),
        output="", workspace=None, usage={}, deferred=True,
    )
    response = await f.service.resolve(None, f.product.principal, pending["request_id"], provide())
    assert response["resumed"]


async def test_resolution_and_resume_need_no_nested_native_pool_checkout(credentials):
    f = credentials
    engine = create_async_engine(
        f.product.database.settings.sqlalchemy_url, pool_size=1, max_overflow=0, pool_timeout=2,
        connect_args={"server_settings": {"search_path": f'"{f.product.schema}",pg_catalog'}},
    )
    previous_sessions, previous_engine = f.product.database.sessions, f.product.adapter.native_engine
    f.product.database.sessions = async_sessionmaker(engine, expire_on_commit=False)
    f.product.adapter.native_engine = engine
    try:
        pending = await asyncio.wait_for(wait_for_credential(f), 5)
        result = await asyncio.wait_for(f.service.resolve(None, f.product.principal, pending["request_id"], provide()), 5)
        assert result["resumed"]
    finally:
        f.product.database.sessions, f.product.adapter.native_engine = previous_sessions, previous_engine
        await engine.dispose()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_source_lookup_claim_requires_confirmed_command_with_same_handle(credentials, wrapped):
    f = credentials
    source = CredentialRequest(
        provider="generic", name="1password-shared", reason="Read the approved shared vault.",
        request_key="vault-source", input_fields=[{"name": "OP_SERVICE_ACCOUNT_TOKEN", "label": "Vault token"}],
    )
    pending = await wait_for_credential(f, source)
    response = await f.service.resolve(None, f.product.principal, pending["request_id"], Resolve(
        generation=0, scope="personal", lifetime="persistent",
        value=SecretStr('{"OP_SERVICE_ACCOUNT_TOKEN":"synthetic-vault-token"}'),
    ))
    await f.product.database.claim_next()
    scope = (await f.product.database.prepare(response["turn_id"])).scope
    needed = await f.service.request(scope, scope.turn_id, "target", request_body())
    assert needed["status"] == "lookup_required"
    metadata = needed["credential_sources"][0]
    checked = request_body(source_checks=[{
        "secret_id": metadata["id"], "revision": metadata["revision"], "outcome": "not_found",
    }])
    with pytest.raises(HTTPException, match="confirmed credentials_run"):
        await f.service.request(scope, scope.turn_id, "target", checked)
    identity = "a" * 64
    arguments = {"request_ids": [pending["request_id"]], "command": "op item list"}
    await EffectReceipts(f.product.database).reserve(
        scope, identity, "credentials_run", {"request": arguments} if wrapped else arguments,
    )
    with pytest.raises(HTTPException, match="confirmed credentials_run"):
        await f.service.request(scope, scope.turn_id, "target", checked)
    await confirm_effect(f.product.database, identity)
    assert (await f.service.request(scope, scope.turn_id, "target", checked))["status"] == "pending"


async def test_session_lifetime_credential_follows_only_its_fenced_original_root(credentials):
    f = credentials
    pending = await wait_for_credential(f)
    response = await f.service.resolve(None, f.product.principal, pending["request_id"], provide().model_copy(update={
        "lifetime": "session",
    }))
    await f.product.database.claim_next()
    scope = (await f.product.database.prepare(response["turn_id"])).scope
    original, root = "a" * 32, "b" * 32
    async with f.product.source.begin() as db:
        await db.execute(text("CREATE TABLE moyai.run_roots(run_id TEXT PRIMARY KEY,root_id TEXT)"))
        await db.execute(text("INSERT INTO moyai.run_roots VALUES(:run,:root)"), {"run": original, "root": root})
        await db.execute(text("UPDATE moyai.provider_secrets SET root_id=:root"), {"root": root})
    assert not (await f.service.inventory(scope, ListCredentials()))["credentials"]
    async with f.product.database.sessions.begin() as db:
        db.add(ProductContinuation(
            source_schema="moyai", source_session_id=original, native_session_id=f.session.id,
            source_owner_id=f.product.user_id, initiated_by=f.product.user_id,
            source_fingerprint="1" * 64, history=f.runtime.security.seal([]), history_count=0, imported_count=0,
            workspace=f.runtime.security.seal({}), workspace_fingerprint="2" * 64, first_turn_id=f.turn.id,
        ))
    assert len((await f.service.inventory(scope, ListCredentials()))["credentials"]) == 1
    async with f.product.source.begin() as db:
        await db.execute(text("UPDATE moyai.provider_secrets SET root_id=:root"), {"root": "c" * 32})
    assert not (await f.service.inventory(scope, ListCredentials()))["credentials"]


def test_command_output_redacts_direct_and_common_encoded_material():
    token = "synthetic-example-token"
    bindings = [{"format": "env", "value": json.dumps({"EXAMPLE_TOKEN": token})}]
    output = redact_output(token + " " + base64.b64encode(token.encode()).decode(), bindings)
    assert token not in output and output == "[credential redacted] [credential redacted]"


async def test_root_credential_request_survives_history_segment_without_allowing_other_runs(credentials):
    f = credentials
    scope = replace(f.scope, history_segment=5)
    result = await f.service.request(scope, scope.root_run_id, "continued-credential", request_body())
    assert result["status"] == "pending"
    for foreign in ["another-root", f.scope.turn_id, f.scope.turn_id + ":segment:4"]:
        with pytest.raises((HTTPException, PermissionError)):
            await f.service.request(scope, foreign, "foreign-credential", request_body())
