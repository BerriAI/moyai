"""Real PostgreSQL journal tests with HTTP/Git boundary doubles; no E2E claim."""

from __future__ import annotations

import asyncio
import copy
import os
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
import pytest_asyncio
import test_product_github_publication as support
from app.connector_errors import ConnectorError
from fastapi import HTTPException
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel

from moyai import gitlab_transport
from moyai.contracts import Grant, NewMessage, NewSession
from moyai.gitlab_request_policy import REQUEST_CHANGES, REVIEW_CAPABILITY
from moyai.gitlab_workflow_types import (
    AddToStack,
    CreateBranch,
    CreateMR,
    CreateReview,
    CreateStack,
    EditReview,
    MRWrite,
    OpenMR,
    PublishBranch,
    RebaseMR,
    Retarget,
    StackMember,
    StackRead,
    Status,
    SubmitReview,
    UpdateMR,
)
from moyai.models import Effect
from moyai.product_connector_execution import CanonicalConnectorExecution
from moyai.product_github_models import ProductGitOperation as Operation
from moyai.product_gitlab import ProductGitLabToolset
from moyai.product_gitlab_publication import Publications
from moyai.product_gitlab_reviews import Reviews
from moyai.product_gitlab_stacks import Stacks
from moyai.product_gitlab_workflows import Workflows

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = support.product_database
admitted_product = support.admitted_product
BASE = "a" * 40


class Provider:
    def __init__(self):
        self.calls, self.refs, self.mrs, self.drafts = [], {"main": BASE}, {}, {}
        self.rules = []
        self.lost_push = self.lost_mr = self.race = False
        self.fail_publish = None
        self.next_note = 1
        self.reviews_supported = True

    def mr(self, number):
        value = copy.deepcopy(self.mrs[number])
        value["sha"] = self.refs[value["source_branch"]]
        value["diff_refs"] = {"base_sha": BASE, "head_sha": value["sha"], "start_sha": BASE}
        return value

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        prefix = "/projects/123"
        if path == "/graphql":
            body = kwargs["json"]
            if body == {"query": REVIEW_CAPABILITY}:
                return {"data": {"__type": {"fields": [{"name": "mergeRequestRequestChanges"}] if self.reviews_supported else []}}}
            assert body["query"] == REQUEST_CHANGES
            mr = self.mr(int(body["variables"]["iid"]))
            return {"data": {"mergeRequestRequestChanges": {"errors": [], "mergeRequest": {
                "id": f"gid://gitlab/MergeRequest/{mr['id']}", "iid": str(mr["iid"]), "projectId": 123,
                "sourceProjectId": 123, "targetProjectId": 123, "diffHeadSha": mr["sha"],
            }}}}
        if path == prefix:
            return {"id": 123, "path_with_namespace": "group/sub/repo", "default_branch": "main",
                    "http_url_to_repo": "https://gitlab.com/group/sub/repo.git",
                    "web_url": "https://gitlab.com/group/sub/repo"}
        if path.startswith(prefix + "/repository/commits/"):
            ref = unquote(path.rsplit("/", 1)[-1])
            return {"id": self.refs.get(ref, ref)}
        if path.startswith(prefix + "/repository/branches/"):
            branch = unquote(path.removeprefix(prefix + "/repository/branches/"))
            return {"name": branch, "protected": branch == "main", "commit": {"id": self.refs[branch]}} if branch in self.refs else None
        if path == prefix + "/protected_branches":
            return copy.deepcopy(self.rules)
        if path == prefix + "/merge_requests":
            body = kwargs.get("json", {})
            if method == "GET":
                params = kwargs.get("params", {})
                return [self.mr(number) for number, mr in self.mrs.items() if (
                    not params or mr["source_branch"] == params["source_branch"] and mr["target_branch"] == params["target_branch"]
                )]
            assert method == "POST" and body["remove_source_branch"] is False
            number = len(self.mrs) + 7
            self.mrs[number] = {
                "id": number + 1000, "iid": number, "project_id": 123, "source_project_id": 123,
                "target_project_id": 123, "source_branch": body["source_branch"], "target_branch": body["target_branch"],
                "state": "opened", "draft": False, "title": body["title"],
                "web_url": f"https://gitlab.com/group/sub/repo/-/merge_requests/{number}", "merged_at": None,
            }
            if self.lost_mr:
                raise ConnectorError("Lost MR acknowledgment", dispatched=True)
            return self.mr(number)
        parts = path.removeprefix(prefix + "/merge_requests/").split("/")
        number = int(parts[0])
        if len(parts) == 1:
            if method == "PUT":
                for key, value in kwargs["json"].items():
                    if key == "state_event":
                        self.mrs[number]["state"] = {"close": "closed", "reopen": "opened"}[value]
                    else:
                        self.mrs[number][key] = value
                        if key == "title":
                            self.mrs[number]["draft"] = value.startswith("Draft: ")
            return self.mr(number)
        assert parts[1] == "draft_notes"
        if len(parts) == 2:
            assert method == "POST"
            body = kwargs["json"]
            note = {"id": self.next_note, "author_id": 9, "merge_request_id": self.mrs[number]["id"],
                    "note": body["note"], "position": body.get("position"), "commit_id": body.get("commit_id"),
                    "resolve_discussion": False, "discussion_id": None}
            self.next_note += 1
            self.drafts[note["id"]] = note
            return copy.deepcopy(note)
        identity = int(parts[2])
        if len(parts) == 4:
            assert parts[3] == "publish" and method == "PUT"
            del self.drafts[identity]
            if self.fail_publish == identity:
                raise ConnectorError("Lost draft publication acknowledgment", dispatched=True)
            return {}
        if method == "DELETE":
            del self.drafts[identity]
            return {}
        if method == "PUT":
            self.drafts[identity]["note"] = kwargs["json"]["note"]
        return copy.deepcopy(self.drafts[identity])


@pytest_asyncio.fixture
async def native(admitted_product, monkeypatch):
    product = admitted_product
    grant = Grant("22222222-2222-2222-2222-222222222222", "gitlab", 1, "", read_only=False,
                  policy={"product": {"repository_id": 123, "base_url": "https://gitlab.com"}})
    state = {"allowed": True}
    async def resolve(actor_id, selections, **kwargs):
        if not state["allowed"]:
            raise HTTPException(403, "Revoked")
        return [grant] if selections else []
    product.database.product_connections = SimpleNamespace(resolve_grants=resolve)
    session = await product.database.create_session(product.actor, NewSession())
    turn = await product.database.submit(product.actor, session.id, NewMessage(
        prompt="GitLab protocol test", request_key="gitlab-protocol", connections=[grant.id],
    ))
    await product.database.claim_next()
    scope = (await product.database.prepare(turn.id)).scope
    provider = Provider()
    async def check(scope, selected):
        assert selected == grant
        await product.database.authorize_scope(scope)
    async def credentials(scope, selected, **kwargs):
        await check(scope, selected)
        return {"access_token": "synthetic-token", "base_url": "https://gitlab.com"}
    service = SimpleNamespace(
        database=product.database, product=SimpleNamespace(identity=product.adapter), check=check, credentials=credentials,
        connectors=SimpleNamespace(
            gitlab=SimpleNamespace(request=provider.request, allow_loopback_http=False), github=SimpleNamespace(request=provider.request),
            personal_slack=SimpleNamespace(), locks={"gitlab": asyncio.Lock()}, request=provider.request,
        ),
    )
    service.execution = CanonicalConnectorExecution(service)
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage(), run_id=turn.id, tool_call_id="create")
    async def prepare(**kwargs):
        import hashlib
        return gitlab_transport.PreparedCommit(
            hashlib.sha1((kwargs["base_sha"] + kwargs["title"]).encode()).hexdigest(), b"test-bundle",
        )
    async def push(**kwargs):
        branch = kwargs["branch"]
        if provider.race:
            provider.refs[branch] = "9" * 40
        if provider.refs.get(branch) != kwargs["expected_head"]:
            raise ToolFailed("The exact ref lease was rejected.")
        provider.refs[branch] = kwargs["commit"]
        if provider.lost_push:
            raise ConnectorError("Lost push acknowledgment", dispatched=True)
        return {"provider": "gitlab", "origin": kwargs["base_url"], "project": kwargs["project_path"],
                "branch": branch, "before": kwargs["expected_head"], "commit": kwargs["commit"], "acknowledged": True}
    monkeypatch.setattr(gitlab_transport, "prepare_commit", prepare)
    monkeypatch.setattr(gitlab_transport, "push", push)
    return SimpleNamespace(product=product, service=service, scope=scope, ctx=ctx, grant=grant,
                           publisher=Publications(service, scope, grant), provider=provider, state=state)


async def invoke(f, kind, request):
    f.ctx.tool_call_id = kind + request.request_key
    await support.reserve(f, kind, request, name=f"gitlab_{f.grant.id[:8]}_{kind}")
    w, p = Workflows(f.publisher), f.publisher
    if kind == "create_branch":
        return await w.create_branch(f.ctx, request)
    if kind == "open_merge_request":
        return await w.open_existing(f.ctx, request)
    if kind in {"create_merge_request", "publish_branch", "update_merge_request", "rebase_merge_request"}:
        return await w.publish(f.ctx, kind, request)
    if kind in {"create_review", "submit_review", "edit_pending_review", "delete_pending_review"}:
        return await Reviews(p).run(f.ctx, kind, request)
    if kind in {"create_stack", "add_to_stack"}:
        return await Stacks(p).write(f.ctx, kind, request)
    return await w.lifecycle(f.ctx, kind, request)


async def owned(f, **kwargs):
    return await invoke(f, "create_merge_request", CreateMR(
        repository_id=123, request_key=kwargs.pop("request_key", "create-mr"),
        base_sha=kwargs.pop("base_sha", BASE), title=kwargs.pop("title", "Native GitLab MR"),
        body="Scoped changes", files=[{"path": "hello.txt", "content": "Hello"}], **kwargs,
    ))


async def test_regular_mr_update_rebase_and_exact_lineage(native):
    f = native
    mr = await owned(f)
    assert mr["draft"] is False and mr["iid"] == 7
    assert await Workflows(f.publisher).branch_owned(mr["source_branch"], mr["sha"])
    updated = await invoke(f, "update_merge_request", UpdateMR(
        request_key="update-mr", number=7, base_branch="main", base_sha=mr["sha"],
        title="Followup change", files=[{"path": "hello.txt", "content": "updated"}],
    ))
    assert await Workflows(f.publisher).branch_owned(mr["source_branch"], updated["commit"])
    rebased = await invoke(f, "rebase_merge_request", RebaseMR(
        request_key="rebase-mr", number=7, base_branch="main", base_sha=BASE,
        expected_head_sha=updated["commit"], title="Prepared base change", files=[],
    ))
    assert rebased["commit"] != updated["commit"]
    assert await Workflows(f.publisher).branch_owned(mr["source_branch"], rebased["commit"])
    f.provider.refs[mr["source_branch"]] = "9" * 40
    assert not await Workflows(f.publisher).branch_owned(mr["source_branch"], "9" * 40)


async def test_uncertain_push_never_replayed_and_freezes_request(native):
    f = native
    request = PublishBranch(request_key="lost-push", branch="feature", base_sha=BASE,
                            title="Publish branch", files=[{"path": "x", "content": "x"}])
    f.provider.lost_push = True
    with pytest.raises(ToolFailed, match="may already"):
        await invoke(f, "publish_branch", request)
    f.provider.lost_push = False
    with pytest.raises(ToolFailed):
        await invoke(f, "publish_branch", request)
    status = await f.publisher.status(Status(request_key="lost-push", operation="publish_branch"))
    assert status["uncertain_steps"] == ["push"] and status["status"] == "incomplete"
    with pytest.raises((ToolFailed, ValueError), match="different|cannot change"):
        await invoke(f, "publish_branch", request.model_copy(update={"title": "Different title"}))


async def test_acknowledged_push_recovers_without_reading_mutable_provider(native):
    f = native
    request = CreateBranch(request_key="create-ref", branch="feature", commit_sha=BASE)
    result = await invoke(f, "create_branch", request)
    identity = f.publisher.operation_id("create_branch", request.request_key)
    async with f.product.database.sessions.begin() as db:
        row = await db.get(Operation, identity)
        document = f.publisher.document(row)
        document.pop("result")
        row.document = f.product.database.security.seal(document)
        effect = await db.get(Effect, row.original_effect_id)
        effect.state = "pending"
    f.provider.refs.pop("feature")
    f.provider.calls.clear()
    assert await invoke(f, "create_branch", request) == result
    assert f.provider.calls == []


async def test_preflight_refusal_proven_no_write_and_no_ownership_from_open(native):
    f = native
    f.provider.refs["external"] = BASE
    await invoke(f, "open_merge_request", OpenMR(
        request_key="open-existing", base_branch="main", head_branch="external",
        expected_head_sha=BASE, title="Open existing", body="Existing branch",
    ))
    with pytest.raises(ToolFailed, match="approval"):
        await invoke(f, "update_merge_request", UpdateMR(
            request_key="external-update", number=7, base_branch="main", base_sha=BASE,
            title="External update", files=[{"path": "x", "content": "x"}],
        ))
    assert not await Workflows(f.publisher).branch_owned("external", BASE)
    f.provider.rules = [{"name": "blocked/*", "inherited": True}]
    with pytest.raises(ToolFailed, match="No repository write"):
        await invoke(f, "create_branch", CreateBranch(request_key="protected-create", branch="blocked/new", commit_sha=BASE))
    status = await f.publisher.status(Status(request_key="protected-create", operation="create_branch"))
    assert status["status"] == "not_dispatched" and not status["attempted_steps"]


async def test_external_exact_head_approval_and_lifecycle_separation(native):
    f = native
    f.provider.refs["external"] = BASE
    await invoke(f, "open_merge_request", OpenMR(request_key="open-external", base_branch="main", head_branch="external",
                                                expected_head_sha=BASE, title="Open existing", body="Description"))
    # Strip creation authority to model a pre-existing MR.
    async with f.product.database.sessions.begin() as db:
        row = await db.get(Operation, f.publisher.operation_id("open_merge_request", "open-external"))
        document = f.publisher.document(row)
        document["revoked"] = True
        row.document = f.product.database.security.seal(document)
    request = MRWrite(request_key="external-consent", number=7, head_sha=BASE, base_branch="main")
    f.ctx.tool_call_id = "approve"
    await support.reserve(f, "request_merge_request_write_access", request,
                          name=f"gitlab_{f.grant.id[:8]}_request_merge_request_write_access")
    with pytest.raises(ToolFailed, match="explicit approval"):
        await f.publisher.approve(f.ctx, request)
    f.ctx.tool_call_approved = True
    await f.publisher.approve(f.ctx, request)
    with pytest.raises(ToolFailed, match="lifecycle"):
        await invoke(f, "close_merge_request", MRWrite(request_key="close-external", number=7, head_sha=BASE, base_branch="main"))
    result = await invoke(f, "update_merge_request", UpdateMR(
        request_key="approved-update", number=7, base_sha=BASE, base_branch="main",
        title="Approved update", files=[{"path": "x", "content": "x"}],
    ))
    assert result["commit"] != BASE


async def test_lifecycle_retarget_then_close_reopen_draft(native):
    f = native
    mr = await owned(f)
    f.provider.refs["release"] = BASE
    after = await invoke(f, "retarget_merge_request", Retarget(
        request_key="retarget-mr", number=7, head_sha=mr["sha"], base_branch="main", target_branch="release",
    ))
    assert after["target_branch"] == "release"
    for kind, state in [("close_merge_request", "closed"), ("reopen_merge_request", "opened"), ("convert_merge_request_to_draft", "opened")]:
        after = await invoke(f, kind, MRWrite(request_key=kind, number=7, head_sha=mr["sha"], base_branch="release"))
        assert after["state"] == state
    assert after["draft"] is True


async def test_task_review_isolation_edit_and_partial_publication_receipt(native):
    f = native
    mr = await owned(f)
    pending = await invoke(f, "create_review", CreateReview(
        request_key="pending-review", number=7, head_sha=mr["sha"], base_branch="main",
        body="Summary", comments=[{"body": "Second note"}],
    ))
    f.provider.drafts[999] = {"id": 999, "note": "Another task"}
    await invoke(f, "edit_pending_review", EditReview(
        request_key="edit-review", number=7, head_sha=mr["sha"], base_branch="main",
        review_id=pending["review_id"], note_id=1, body="Edited summary",
    ))
    assert f.provider.drafts[1]["note"] == "Edited summary"
    f.provider.fail_publish = 2
    request = SubmitReview(request_key="submit-review", number=7, head_sha=mr["sha"], base_branch="main",
                           review_id=pending["review_id"], event="COMMENT")
    with pytest.raises(ToolFailed, match="may already"):
        await invoke(f, "submit_review", request)
    assert 999 in f.provider.drafts
    status = await f.publisher.status(Status(request_key="submit-review", operation="submit_review"))
    assert "publish-note-1" in status["confirmed_steps"] and status["uncertain_steps"] == ["publish-note-2"]
    assert all("bulk_publish" not in path for _, path, _ in f.provider.calls)


async def test_real_request_changes_and_runtime_unavailable_never_comment_substitute(native):
    f = native
    mr = await owned(f)
    request = CreateReview(request_key="request-changes", number=7, head_sha=mr["sha"], base_branch="main",
                           body="Please fix the issue", event="REQUEST_CHANGES")
    f.provider.reviews_supported = False
    with pytest.raises(ToolFailed, match="unavailable"):
        await invoke(f, "create_review", request)
    assert not f.provider.drafts
    f.provider.reviews_supported = True
    request = request.model_copy(update={"request_key": "request-changes-supported"})
    result = await invoke(f, "create_review", request)
    assert result["state"] == "CHANGES_REQUESTED"
    assert any(kwargs.get("json", {}).get("query") == REQUEST_CHANGES for _, _, kwargs in f.provider.calls)


async def test_managed_stack_full_snapshot_and_append_conflict(native):
    f = native
    first = await owned(f, head_branch="lower")
    second = await owned(f, request_key="second-mr", base_sha=first["sha"], base_branch="lower", head_branch="upper")
    def member(value):
        return StackMember(number=value["iid"], head_sha=value["sha"],
                           head_branch=value["source_branch"], base_branch=value["target_branch"])
    created = await invoke(f, "create_stack", CreateStack(
        request_key="stack-create", merge_requests=[member(first), member(second)],
    ))
    snapshot = await Stacks(f.publisher).read(StackRead(stack_id=created["stack_id"]))
    assert snapshot["revision"] == created["revision"]
    third = await owned(f, request_key="third-mr", base_sha=second["sha"], base_branch="upper", head_branch="top")
    request = AddToStack(request_key="stack-add", stack_id=created["stack_id"], revision=snapshot["revision"],
                         members=snapshot["members"], merge_requests=[member(third)])
    result = await invoke(f, "add_to_stack", request)
    assert result["kind"] == "moyai_managed_mr_chain" and len(result["members"]) == 3
    with pytest.raises(ToolFailed, match="changed"):
        await invoke(f, "add_to_stack", request.model_copy(update={"request_key": "stack-stale"}))


async def test_actual_toolset_validator_dispatch_and_target_fence(native):
    f = native
    tools = ProductGitLabToolset(f.service, f.scope, f.grant)
    catalog = await tools.get_tools(f.ctx)
    definition = catalog["repository"]
    args = definition.args_validator.validate_python({"repository_id": 123})
    result = await tools.call_tool("repository", args, f.ctx, definition)
    assert result["id"] == 123
    args = definition.args_validator.validate_python({"repository_id": 124})
    with pytest.raises(ToolFailed, match="different"):
        await tools.call_tool("repository", args, f.ctx, definition)


@pytest.mark.parametrize("changed", ["head", "base"])
async def test_public_toolset_stale_owned_mr_is_not_external_consent(native, monkeypatch, changed):
    from unittest.mock import AsyncMock

    f = native
    original = await owned(f)
    if changed == "head":
        await invoke(f, "update_merge_request", UpdateMR(
            request_key="advance-owned-head", number=original["iid"], base_branch="main", base_sha=original["sha"],
            title="Acknowledged follow-up", files=[{"path": "hello.txt", "content": "updated"}],
        ))
    else:
        f.provider.refs["release"] = BASE
        await invoke(f, "retarget_merge_request", Retarget(
            request_key="retarget-owned-base", number=original["iid"], head_sha=original["sha"],
            base_branch="main", target_branch="release",
        ))
    request = UpdateMR(
        request_key="stale-owned-" + changed, number=original["iid"], base_branch="main", base_sha=original["sha"],
        title="Refuse stale inputs", files=[{"path": "hello.txt", "content": "stale publication"}],
    )
    f.ctx.tool_call_id = request.request_key
    await support.reserve(f, "update_merge_request", request,
                          name=f"gitlab_{f.grant.id[:8]}_update_merge_request")
    tools = ProductGitLabToolset(f.service, f.scope, f.grant)
    definition = (await tools.get_tools(f.ctx))["update_merge_request"]
    arguments = definition.args_validator.validate_python(request.model_dump(mode="json"))
    prepared, pushed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(gitlab_transport, "prepare_commit", prepared)
    monkeypatch.setattr(gitlab_transport, "push", pushed)
    before = copy.deepcopy((f.provider.refs, f.provider.mrs, f.provider.drafts))
    f.provider.calls.clear()
    with pytest.raises(ToolFailed, match="MR head, base or unmerged state changed") as refused:
        await tools.call_tool("update_merge_request", arguments, f.ctx, definition)
    assert "approval" not in str(refused.value) and "external MR" not in str(refused.value)
    prepared.assert_not_awaited()
    pushed.assert_not_awaited()
    assert all(method == "GET" for method, _, _ in f.provider.calls)
    assert (f.provider.refs, f.provider.mrs, f.provider.drafts) == before
    status = await f.publisher.status(Status(operation="update_merge_request", request_key=request.request_key))
    assert status["status"] == "not_dispatched"
    assert status["attempted_steps"] == status["confirmed_steps"] == []


async def test_review_saved_ack_recovers_after_head_change_without_provider_reads(native):
    f = native
    mr = await owned(f)
    request = CreateReview(request_key="review-ack", number=7, head_sha=mr["sha"], base_branch="main",
                           body="Verified comment", event="COMMENT")
    result = await invoke(f, "create_review", request)
    async with f.product.database.sessions.begin() as db:
        row = await db.get(Operation, result["review_id"])
        document = f.publisher.document(row)
        for key in ("result", "terminal_result", "terminal_notes"):
            document.pop(key, None)
        row.document = f.product.database.security.seal(document)
        effect = await db.get(Effect, row.original_effect_id)
        effect.state = "pending"
    f.provider.refs[mr["source_branch"]] = "9" * 40
    f.provider.calls.clear()
    assert await invoke(f, "create_review", request) == result
    assert f.provider.calls == []


async def test_staged_files_frozen_before_provider_effect_and_never_reexported(native):
    import json
    from unittest.mock import AsyncMock
    f = native
    f.ctx.workspace = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(
        exit_code=0, stdout=json.dumps({"files": [{"path": "staged.bin", "content": "AP8=", "encoding": "base64",
                                                 "executable": True}, {"path": "old.txt", "content": None}]}),
    )))
    request = PublishBranch(request_key="staged-export", branch="feature", base_sha=BASE,
                            title="Publish staged", directory="/workspace/repo")
    result = await invoke(f, "publish_branch", request)
    assert await invoke(f, "publish_branch", request) == result
    assert f.ctx.workspace.run.await_count == 1
    async with f.product.database.sessions() as db:
        row = await db.get(Operation, f.publisher.operation_id("publish_branch", request.request_key))
    assert f.publisher.document(row)["files"][0]["encoding"] == "base64"
