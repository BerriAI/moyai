"""A real Slack principal can delegate without gaining private human sessions."""

import os
from types import SimpleNamespace

import pytest
import test_product_authority as authority
from fastapi import HTTPException
from test_product_identity_admission import slack_admission as slack_admission  # noqa: PLC0414 - shared pytest fixture
from test_product_memory import memory_product as memory_product  # noqa: PLC0414 - shared pytest fixture

from moyai.contracts import NewMessage, NewSession
from moyai.delegation import DelegationService
from moyai.delegation_models import DelegatedSession
from moyai.models import ProductTurnAuthority, Session
from moyai.product_slack_models import ProductSlackBinding
from moyai.slack_audience import can_read

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = authority.product_database
admitted_product = authority.admitted_product


async def test_slack_delegation_keeps_original_authority_and_private_audience(slack_admission):
    product = slack_admission
    database, principal = product.database, product.raw_principal
    actor = await database.user(principal.id)
    actor.product_grant = principal.grant
    assert not principal.individual
    with pytest.raises(HTTPException) as refusal:
        await database.create_session(actor, NewSession(visibility="private"))
    assert refusal.value.status_code == 403
    _, _, prepared = await slack_parent(product)
    child = await DelegationService(database).start(prepared.scope, "child-once", "Inspect the UI.")
    assert await DelegationService(database).start(prepared.scope, "child-once", "Inspect the UI.") == child
    async with database.sessions() as db:
        delegated = await db.get(Session, child["session_id"])
        assert delegated.visibility == "private" and delegated.owner_id == principal.id
        child_turn = (await db.get(DelegatedSession, delegated.id)).initial_turn_id
        saved = await db.get(ProductTurnAuthority, child_turn)
        assert saved.actor_id == principal.id
    assert (await database.claim_next()).id == child_turn
    child_scope = (await database.prepare(child_turn)).scope
    assert child_scope.product_grant == principal.grant
    product.adapter.settings.slack_session_users = "UREVOKED"
    with pytest.raises((HTTPException, PermissionError)):
        await database.authorize_scope(child_scope)


async def slack_parent(product):
    database, principal = product.database, product.raw_principal
    actor = await database.user(principal.id)
    actor.product_grant = principal.grant
    parent = await database.create_session(actor, NewSession(visibility="shared"))
    async def installation_in(db):
        assert db.in_transaction()
        return {"team_id": "TLOCAL001", "authority": "a" * 64}
    slack = SimpleNamespace(runtime=SimpleNamespace(database=database),
                            installation_in=installation_in)
    database.slack_access = lambda db, current, viewer: can_read(slack, db, current, viewer)
    async with database.sessions.begin() as db:
        (await db.get(Session, parent.id)).surface = "slack"
        db.add(ProductSlackBinding(session_id=parent.id, requester_id=actor.id, team_id="TLOCAL001",
            channel_id="DTEST0001", thread_ts="1792255740.000", started_ts="1792255740.000",
            connection_authority="a" * 64))
    turn = await database.submit(actor, parent.id, NewMessage(prompt="Inspect the repository.", request_key="parent"))
    assert (await database.claim_next()).id == turn.id
    return actor, parent, await database.prepare(turn.id)


@pytest.mark.parametrize("repository", [False, True])
async def test_real_slack_child_runs_production_memory_hook(slack_admission, memory_product, repository):
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from test_product_memory import note

    from moyai.product_continuation_workspace import ProductWorkspaceCompatibility
    from moyai.product_memory_store import memory_capability

    product = slack_admission
    await product.memory.save(product.principal.grant, note())
    _, _, prepared = await slack_parent(product)
    if repository:
        from moyai.product_continuation_models import ProductExecutionContext
        from moyai.product_continuation_workspace import fingerprint
        context = {"source_schema": "moyai", "repo_url": "https://github.com/BerriAI/moyai", "repository_id": 123}
        context["context_fingerprint"] = fingerprint(context)
        async with product.database.sessions.begin() as db:
            db.add(ProductExecutionContext(turn_id=prepared.scope.turn_id, actor_id=prepared.scope.actor_id,
                descriptor=product.database.security.seal(context), fingerprint=fingerprint(context)))
        await product.memory.save(product.principal.grant, note(
            key="repo-note", request_id="repo-note", content="Run the repository formatter.",
            repo_url="https://github.com/BerriAI/moyai",
        ))
        await product.memory.save(product.principal.grant, note(
            key="other-repo-note", request_id="other-repo-note", content="UNRELATED_REPOSITORY_MEMORY",
            repo_url="https://github.com/other/project",
        ))
    child = await DelegationService(product.database).start(prepared.scope, "memory-child", "Inspect the UI.")
    turn = await product.database.claim_next()
    saved = await product.database.prepare(turn.id)
    assert saved.scope.product_grant == prepared.scope.product_grant
    assert saved.scope.grants == prepared.scope.grants
    workspace = ProductWorkspaceCompatibility(SimpleNamespace(
        database=product.database, security=product.database.security, product_identity=product.adapter,
    ))
    seen = []

    def respond(messages, info):
        seen.append(messages)
        assert "Use the existing formatter." in str(messages)
        if repository:
            assert "Run the repository formatter." in str(messages)
            assert "UNRELATED_REPOSITORY_MEMORY" not in str(messages)
        return ModelResponse(parts=[TextPart("Used the parent's authorized memory.")])

    agent = Agent(FunctionModel(respond), deps_type=type(saved.scope), capabilities=[
        memory_capability(product.memory, repository_scope=workspace.repository_scope),
    ])
    result = await agent.run(saved.prompt, deps=saved.scope, run_id=turn.id)
    assert len(seen) == 1 and result.output == "Used the parent's authorized memory."
    from pydantic_ai import ConversationTypeAdapter
    assert await product.database.save_result(saved.scope,
        conversation=ConversationTypeAdapter.dump_json(result.conversation).decode(),
        output=result.output, workspace=None, usage={})
    await product.database.settle(turn.id)
    report = await DelegationService(product.database).report(prepared.scope, child["session_id"])
    assert report["status"] == "completed" and report["output"] == result.output


@pytest.mark.parametrize("invalid", ["conflict", "stale", "unverified", "ambiguous", "audience"])
async def test_verified_requester_can_open_and_find_slack_child_but_stale_link_cannot(slack_admission, invalid):
    from sqlalchemy import text

    from moyai.product_routes import ProductReadService, Reader

    product = slack_admission
    _, parent, prepared = await slack_parent(product)
    child = await DelegationService(product.database).start(prepared.scope, "read-child", "Inspect the UI.")
    assert (await product.database.session(product.actor, child["session_id"])).id == child["session_id"]
    service = ProductReadService(SimpleNamespace(database=product.database, product_identity=product.adapter))
    reader = Reader(product.actor, product.principal)
    detail = await service.detail(reader, child["session_id"], "native")
    assert detail["session"]["can_manage"] and detail["session"]["parent_session_id"] == parent.id
    page = await service.listing(reader, source="native", scope="mine")
    assert child["session_id"] in [row["id"] for row in page["sessions"]]
    # A mere stored linked_user_id must never grant private child access.
    async with product.source.begin() as db:
        if invalid == "conflict":
            await db.execute(text("UPDATE moyai.users SET profile_conflict=1 WHERE kind='slack'"))
        elif invalid == "stale":
            await db.execute(text("UPDATE moyai.users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE kind='slack'"))
        elif invalid == "unverified":
            await db.execute(text("UPDATE moyai.users SET profile_eligible=0 WHERE kind='slack'"))
        elif invalid == "ambiguous":
            await db.execute(text("INSERT INTO moyai.users(id,kind,email,name,created_at,updated_at) "
                                  "VALUES('google:duplicate','google','admin@example.test','Duplicate','c','u')"))
        else:
            async def denied(db, session, viewer):
                return False
            product.database.slack_access = denied
    with pytest.raises(HTTPException) as refusal:
        await product.database.session(product.actor, child["session_id"])
    assert refusal.value.status_code == 404
    page = await service.listing(reader, source="native")
    assert child["session_id"] not in [row["id"] for row in page["sessions"]]



async def test_child_pins_parent_repository_descriptor_at_admission(slack_admission):
    import json

    from moyai.product_continuation_models import ProductExecutionContext
    from moyai.product_continuation_workspace import fingerprint

    product = slack_admission
    _, _, prepared = await slack_parent(product)
    context = {"repo_url": "https://github.com/BerriAI/moyai", "repository_id": 123, "commit_sha": "a" * 40}
    async with product.database.sessions.begin() as db:
        db.add(ProductExecutionContext(turn_id=prepared.scope.turn_id, actor_id=prepared.scope.actor_id,
            descriptor=product.database.security.seal(context), fingerprint=fingerprint(context)))
    await DelegationService(product.database).start(prepared.scope, "repo-child", "Inspect the repository.")
    turn = await product.database.claim_next()
    async with product.database.sessions.begin() as db:
        parent = await db.get(ProductExecutionContext, prepared.scope.turn_id)
        parent.descriptor = product.database.security.seal({**context, "commit_sha": "b" * 40})
        child = await db.get(ProductExecutionContext, turn.id)
        assert child is not None and child.actor_id == prepared.scope.actor_id
        assert json.loads(product.database.security.unseal(child.descriptor)) == context
        assert child.fingerprint == fingerprint(context)
