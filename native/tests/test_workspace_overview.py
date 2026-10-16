"""Supplementary native-store/API boundaries; real-provider app proof is separate."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent
from sqlalchemy import func, select
from temporalio.testing import WorkflowEnvironment
from test_application_boundaries import sign_in
from test_images_and_computer import image_bytes
from test_sidebar import member_client

from moyai.contracts import NewMessage, NewSession
from moyai.models import Session, SessionLink, SubagentLabel, Turn, User, new_id
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


async def prepared_turn(runtime, actor, session):
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Investigate the task.", request_key=new_id(),
    ))
    assert (await runtime.database.claim_next()).id == turn.id
    await runtime.database.prepare(turn.id)
    await runtime.database.stopped(turn.id)
    return turn


async def test_invalid_mentioned_pr_does_not_hide_workspace_resources(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    await runtime.database.submit(actor, session.id, NewMessage(
        prompt="A pasted address: https://github.com/example/repo/pull/" + "1" * 3000,
        request_key=new_id(),
    ))
    async with sign_in(runtime) as client:
        response = await client.get(f"/api/sessions/{session.id}/workspace")
    assert response.status_code == 200
    assert response.json()["links"] == []


async def native_run(runtime, parent_id, *, terminal="run_completed", run_id=None):
    """Seed through the actual native store, including its native Temporal ID shape."""
    run_id = run_id or f"temporalRun:{new_id()}"
    await runtime.stores.steps.register_run(RunRecord(
        run_id=run_id, parent_run_id=parent_id, agent_name="private-sdk-agent",
        metadata={"credential": "private-run-metadata"},
    ))
    await runtime.stores.steps.append_event(StepEvent(
        run_id=run_id, kind="tool_call_completed", step_index=1,
        tool_name="shell", tool_call_id="private-tool-call",
        metadata={"token": "private-event-metadata"},
    ))
    if terminal:
        await runtime.stores.steps.append_event(StepEvent(
            run_id=run_id, kind=terminal, step_index=2, error="private-provider-error",
        ))
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=run_id, parent_run_id=parent_id, step_index=2,
        messages=[
            ModelRequest(parts=[UserPromptPart("private-delegation-prompt")]),
            ModelResponse(
                run_id=parent_id, parts=[TextPart("private-inherited-parent-answer")],
            ),
            ModelResponse(run_id=run_id, parts=[
                ThinkingPart("private-reasoning"),
                ToolCallPart("shell", {"token": "private-tool-credential"}, tool_call_id="call"),
            ]),
            ModelRequest(parts=[ToolReturnPart(
                "shell", "private-tool-return https://github.com/private/repository/pull/999",
                tool_call_id="call",
            )]),
            ModelResponse(run_id=run_id, parts=[TextPart("Visible delegate answer.")]),
        ],
    ))
    return run_id


async def test_nested_colon_ids_follow_requester_ancestry_and_keep_stable_public_names(runtime, actor):
    shared = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    private = await runtime.database.create_session(actor, NewSession())
    async with sign_in(runtime) as owner, member_client(runtime) as (member, requester):
        owner_turn = await prepared_turn(runtime, actor, shared)
        member_turn = await prepared_turn(runtime, requester, shared)
        private_turn = await prepared_turn(runtime, actor, private)
        owner_run = await native_run(runtime, owner_turn.id)
        member_run = await native_run(runtime, member_turn.id)
        nested_run = await native_run(runtime, member_run)
        private_run = await native_run(runtime, private_turn.id)
        await native_run(runtime, None, run_id="unrelated-orphan")
        path = f"/api/sessions/{shared.id}"
        first, second, repeated = [
            (await client.get(f"{path}/workspace")).json()
            for client in (owner, member, member)
        ]
        owner_rows = {row["id"]: row for row in first["subagents"]}
        member_rows = {row["id"]: row for row in second["subagents"]}
        assert set(owner_rows) == set(member_rows) == {owner_run, member_run, nested_run}
        assert {row["id"]: row["name"] for row in repeated["subagents"]} == {
            key: row["name"] for key, row in member_rows.items()
        }
        assert all(owner_rows[key]["name"] == row["name"] for key, row in member_rows.items())
        assert all(row["name"] and row["name"] != "private-sdk-agent" for row in member_rows.values())
        assert owner_rows[owner_run]["can_inspect"]
        assert not owner_rows[member_run]["can_inspect"]
        assert not member_rows[owner_run]["can_inspect"]
        assert member_rows[member_run]["can_inspect"] and member_rows[nested_run]["can_inspect"]
        assert member_rows[nested_run]["parent_id"] == member_run
        assert member_rows[nested_run]["depth"] == member_rows[member_run]["depth"] + 1
        assert all(row["status"] == "completed" for row in member_rows.values())
        assert "private-" not in str(second)

        for run_id in (member_run, nested_run):
            response = await member.get(f"{path}/subagents/{run_id}")
            assert response.status_code == 200, response.text
            detail = response.json()
            assert detail["id"] == run_id
            assert detail["output"] == "Visible delegate answer."
            assert {step["kind"] for step in detail["steps"]} == {
                "tool_call_completed", "run_completed",
            }
            for secret in (
                "private-reasoning", "private-tool-credential", "private-tool-return",
                "private-inherited-parent-answer", "private-delegation-prompt",
                "private-event-metadata", "private-run-metadata", "private-provider-error",
            ):
                assert secret not in response.text
            # Session ownership and even the admin role do not grant another requester's details.
            assert (await owner.get(f"{path}/subagents/{run_id}")).status_code == 404
        for run_id in (owner_run, private_run, owner_turn.id, "unrelated-orphan", "missing"):
            assert (await member.get(f"{path}/subagents/{run_id}")).status_code == 404
        assert (await member.get(f"/api/sessions/{private.id}/workspace")).status_code == 404


async def test_cyclic_or_unrooted_lineage_never_authorizes_from_conversation_id(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    for run_id, parent in (("cycle-a", "cycle-b"), ("cycle-b", "cycle-a"), ("orphan", None)):
        await runtime.stores.steps.register_run(RunRecord(
            run_id=run_id, parent_run_id=parent, conversation_id=session.id,
        ))
    async with sign_in(runtime) as client:
        for run_id in ("cycle-a", "cycle-b", "orphan", "bad..id", "bad%5Cid"):
            response = await client.get(f"/api/sessions/{session.id}/subagents/{run_id}")
            assert response.status_code == 404
        assert (await client.get(f"/api/sessions/{session.id}/workspace")).json()["subagents"] == []


async def test_parallel_overviews_allocate_one_stable_label_per_native_run(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await prepared_turn(runtime, actor, session)
    ids = [await native_run(runtime, turn.id) for _ in range(3)]
    before = await runtime.stores.steps.list_runs(parent_run_id=turn.id)
    async with sign_in(runtime) as client:
        responses = await asyncio.gather(*(
            client.get(f"/api/sessions/{session.id}/workspace") for _ in range(4)
        ))
        assert all(response.status_code == 200 for response in responses)
        labels = [
            {row["id"]: (row["name"], row["avatar"]) for row in response.json()["subagents"]}
            for response in responses
        ]
        assert all(value == labels[0] for value in labels)
        assert set(labels[0]) == set(ids)
        assert len({name for name, _ in labels[0].values()}) == len(ids)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SubagentLabel).where(
            SubagentLabel.session_id == session.id,
        )) == len(ids)
    assert await runtime.stores.steps.list_runs(parent_run_id=turn.id) == before


@pytest.mark.parametrize(("parent_status", "terminal", "expected"), [
    ("running", None, "running"),
    ("waiting", None, "waiting"),
    ("completed", None, "interrupted"),
    ("stopped", None, "interrupted"),
    ("running", "run_failed", "failed"),
    ("failed", "run_completed", "completed"),
])
async def test_native_terminal_events_override_parent_status_without_inventing_success(
    runtime, actor, parent_status, terminal, expected,
):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await prepared_turn(runtime, actor, session)
    run_id = await native_run(runtime, turn.id, terminal=terminal)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, turn.id)).status = parent_status
    async with sign_in(runtime) as client:
        result = await client.get(f"/api/sessions/{session.id}/workspace")
        assert result.status_code == 200
        assert [(row["id"], row["status"]) for row in result.json()["subagents"]] == [(run_id, expected)]


async def test_overview_only_uses_public_pr_mentions_and_session_bound_sources(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    async with sign_in(runtime) as client:
        image = (await client.post("/api/uploads?name=bound.png", content=image_bytes())).json()
        unattached = (await client.post("/api/uploads?name=private.png", content=image_bytes())).json()
        turn = await runtime.database.submit(actor, session.id, NewMessage(
            prompt="Review https://github.com/BerriAI/Moyai/pull/465 and "
                   "https://github.com/BerriAI/Moyai/pull/466#discussion.",
            attachments=[image["id"]], request_key="public-prs",
        ))
        await runtime.database.claim_next()
        await runtime.database.prepare(turn.id)
        await native_run(runtime, turn.id)
        path = f"/api/sessions/{session.id}"
        response = await client.post(f"{path}/links", json={
            "kind": "pull_request", "url": "https://GITHUB.COM/BerriAI/Moyai/pull/465/?tab=files#diff",
        })
        assert response.status_code == 201, response.text
        attached = response.json()
        assert attached["url"] == "https://github.com/berriai/moyai/pull/465"
        overview = (await client.get(f"{path}/workspace")).json()
        assert {row["url"] for row in overview["links"]} == {
            "https://github.com/berriai/moyai/pull/465", "https://github.com/berriai/moyai/pull/466",
        }
        assert len(overview["links"]) == 2
        assert next(row for row in overview["links"] if row["url"].endswith("/465"))["origin"] == "attached"
        mentioned = next(row for row in overview["links"] if row["url"].endswith("/466"))
        assert mentioned["origin"] == "mentioned" and not mentioned["can_remove"]
        assert [row["id"] for row in overview["sources"]] == [image["id"]]
        assert unattached["id"] not in str(overview)
        assert (await client.get(overview["sources"][0]["url"])).content == image_bytes()


@pytest.mark.parametrize(("kind", "url"), [
    ("source", "javascript:alert(1)"),
    ("output", "file:///etc/passwd"),
    ("source", "https://user:password@example.com/path"),
    ("source", "https://example.com:65536/path"),
    ("source", "https://example.com/\nsecret"),
    ("pull_request", "https://github.com.evil.test/owner/repo/pull/1"),
    ("pull_request", "https://github.com@evil.test/owner/repo/pull/1"),
    ("pull_request", "https://github.com/owner/repo/issues/1"),
    ("pull_request", "https://github.com/owner/repo/pull/0"),
    ("pull_request", "https://github.com/owner/repo/pull/1/files"),
    ("pull_request", "https://github.com:444/owner/repo/pull/1"),
])
async def test_invalid_links_are_rejected_without_persisting(runtime, actor, kind, url):
    session = await runtime.database.create_session(actor, NewSession())
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/links", json={"kind": kind, "url": url})
        assert response.status_code == 422, response.text
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SessionLink)) == 0


@pytest.mark.parametrize("kind", ["source", "output"])
@pytest.mark.parametrize("byte_length", [2048, 2049])
async def test_unicode_link_limit_counts_utf8_bytes(runtime, actor, kind, byte_length):
    prefix = "https://example.invalid/"
    remaining = byte_length - len(prefix.encode("utf-8"))
    url = prefix + "界" * (remaining // 3) + "x" * (remaining % 3)
    assert len(url) < 2048 and len(url.encode("utf-8")) == byte_length
    session = await runtime.database.create_session(actor, NewSession())
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/links", json={"kind": kind, "url": url})
        assert response.status_code == (201 if byte_length == 2048 else 422), response.text
        if byte_length == 2048:
            assert response.json()["url"] == url
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SessionLink)) == int(byte_length == 2048)


async def test_parallel_link_attachment_is_idempotent_and_remove_is_author_or_owner_only(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    private = await runtime.database.create_session(actor, NewSession())
    async with (
        sign_in(runtime) as owner,
        member_client(runtime) as (author, _),
        member_client(runtime) as (viewer, _),
    ):
        path = f"/api/sessions/{session.id}/links"
        body = {"kind": "source", "url": "http://example.invalid/reference", "title": "Original"}
        responses = await asyncio.gather(*(author.post(path, json=body) for _ in range(4)))
        assert all(response.status_code == 201 for response in responses), [
            response.text for response in responses
        ]
        link_id = responses[0].json()["id"]
        assert {response.json()["id"] for response in responses} == {link_id}
        duplicate = await viewer.post(path, json={**body, "title": "Replacement"})
        assert duplicate.json()["id"] == link_id and duplicate.json()["title"] == "Original"
        assert not duplicate.json()["can_remove"]
        assert (await viewer.delete(f"{path}/{link_id}")).status_code == 403
        assert (await author.delete(f"/api/sessions/{private.id}/links/{link_id}")).status_code == 404
        assert (await owner.delete(f"{path}/{link_id}")).status_code == 200
        recreated = (await author.post(path, json=body)).json()
        assert recreated["id"] != link_id
        assert (await author.delete(f"{path}/{recreated['id']}")).status_code == 200
        assert (await author.post(f"/api/sessions/{private.id}/links", json=body)).status_code == 404


@pytest.mark.parametrize("endpoint", ["workspace", "detail"])
@pytest.mark.parametrize("revocation", ["deleted", "private", "disabled", "token_rotated"])
async def test_native_reads_recheck_authority_after_store_await(
    runtime, actor, monkeypatch, endpoint, revocation,
):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    async with member_client(runtime) as (client, requester):
        turn = await prepared_turn(runtime, requester, session)
        run_id = await native_run(runtime, turn.id)
        entered, release = asyncio.Event(), asyncio.Event()
        method = "list_runs" if endpoint == "workspace" else "latest_snapshot"
        original = getattr(runtime.stores.steps, method)

        async def delayed(**kwargs):
            value = await original(**kwargs)
            entered.set()
            await release.wait()
            return value

        monkeypatch.setattr(runtime.stores.steps, method, delayed)
        suffix = "workspace" if endpoint == "workspace" else f"subagents/{run_id}"
        request = asyncio.create_task(client.get(f"/api/sessions/{session.id}/{suffix}"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with runtime.database.sessions.begin() as db:
                if revocation in {"disabled", "token_rotated"}:
                    user = await db.get(User, requester.id)
                    if revocation == "disabled":
                        user.enabled = False
                    else:
                        user.auth_version += 1
                else:
                    saved = await db.get(Session, session.id)
                    if revocation == "private":
                        saved.visibility = "private"
                    else:
                        saved.deleted = True
            release.set()
            response = await asyncio.wait_for(request, 5)
            assert response.status_code == (401 if revocation in {"disabled", "token_rotated"} else 404)
            assert "Visible delegate answer." not in response.text
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("runtime", ["tools"], indirect=True)
async def test_real_temporal_delegate_is_inspectable_through_workspace_api(runtime, active):
    """Real native execution with a deterministic model, not real-provider E2E proof."""
    session, turn, _ = active
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            result = await asyncio.wait_for(runtime.client.execute_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            ), 45)
    assert result["status"] == "completed"
    children = await runtime.stores.steps.list_runs(parent_run_id=turn.id)
    assert len(children) == 1 and ":" in children[0].run_id
    async with sign_in(runtime) as client:
        path = f"/api/sessions/{session.id}"
        overview = (await client.get(f"{path}/workspace")).json()
        assert overview["subagents"][0]["id"] == children[0].run_id
        assert overview["subagents"][0]["status"] == "completed"
        detail = await client.get(f"{path}/subagents/{children[0].run_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["output"] == "native-child-result"
        assert overview["files"][0]["name"] == "proof.txt"
        assert (await client.get(overview["files"][0]["url"])).content == (
            b"Written by the native file capability.\n"
        )
