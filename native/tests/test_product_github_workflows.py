"""Native SQL/effect regressions; provider fixtures are supplementary to live E2E."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import get_args

import pytest
import test_product_github_publication as support
from app.github import Update
from pydantic import ValidationError
from pydantic_ai.exceptions import ToolFailed
from sqlalchemy import delete, func, select

from moyai.github_request_policy import ADVANCE, PROTECTION, REBASE
from moyai.github_workflow_types import (
    AddToStack,
    AuthorityRef,
    Checkout,
    CreateBranch,
    CreateReview,
    CreateStack,
    EditReview,
    LifecycleAccess,
    OpenPullRequest,
    PendingReview,
    PRWrite,
    PublicationStatus,
    Publish,
    PublishBranch,
    Rebase,
    Retarget,
    StackMember,
    StackRead,
    SubmitReview,
    WorkspacePublishBranch,
)
from moyai.models import Effect
from moyai.pr_publications import PRPublication
from moyai.product_github_models import ProductGitOperation
from moyai.product_github_publication import Publications, toolset
from moyai.product_github_stacks import Stacks
from moyai.product_github_workflows import Workflows

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
admitted_product = support.admitted_product
product_database = support.product_database
publication = support.publication
create_request = support.create_request
reserve = support.reserve


def mutations(provider):
    return [call for call in provider.calls if call[0] != "GET"
            and not (call[1] == "/graphql" and call[2]["json"].get("query") == PROTECTION)]


async def invoke(f, kind, request):
    f.ctx.tool_call_id = kind + request.request_key
    await reserve(f, kind, request)
    workflows = Workflows(f.publisher)
    function = {
        "create_branch": workflows.create_branch, "publish_branch": workflows.publish_branch,
        "open_pull_request": workflows.open_existing, "rebase_pull_request": workflows.rebase,
        "create_pull_request": workflows.create,
    }.get(kind)
    if function:
        return await function(f.ctx, request)
    if kind in {"create_review", "submit_review", "edit_pending_review", "delete_pending_review"}:
        return await workflows.review(f.ctx, kind, request)
    return await workflows.lifecycle(f.ctx, kind, request)


async def owned(f):
    return await invoke(f, "create_pull_request", Publish.model_validate(create_request().model_dump()))


async def test_native_catalog_exposes_typed_workflows_and_readonly_hides_all_writes(publication):
    f = publication
    names = await toolset(f.service, f.scope, f.grant).get_tools(f.ctx)
    assert {"checkout", "create_branch", "publish_branch", "open_pull_request", "rebase_pull_request",
            "retarget_pull_request", "create_review", "submit_review", "stacks", "stack"} <= names.keys()
    assert not {"merge_pull_request", "approve_pull_request", "enable_auto_merge", "unstack"} & names.keys()
    schema = names["create_pull_request"].tool_def.parameters_json_schema
    assert "base_branch" in json.dumps(schema) and "head_branch" in json.dumps(schema)
    assert set(await toolset(f.service, f.scope, replace(f.grant, read_only=True)).get_tools(f.ctx)) == {
        "publication_status", "checkout", "stacks", "stack",
    }
    provider_writes = {
        name for name, tool in names.items()
        if not tool.tool_def.metadata["annotations"]["readOnlyHint"]
        and name not in {"request_pull_request_write_access", "request_pr_lifecycle_access"}
    }
    assert set(get_args(PublicationStatus.model_fields["operation"].annotation)) == provider_writes


async def test_staged_publication_uses_the_existing_native_reservation_and_receipt(publication):
    f = publication
    calls = []

    async def run(command, **options):
        calls.append(command)
        assert command[:3] == ["python3", "-I", "-c"]
        assert command[-2:] == ["/workspace/repo", "a" * 40]
        return SimpleNamespace(exit_code=0, stdout=json.dumps({"files": [{"path": "hello.txt", "content": "Staged content"}]}))

    f.ctx.workspace = SimpleNamespace(run=run)
    request = WorkspacePublishBranch(repository_id=123, directory="/workspace/repo", base_sha="a" * 40,
                                     branch="work/staged", title="Publish the staged edit", request_key="staged-once")
    f.ctx.tool_call_id = "staged-call"
    await reserve(f, "publish_branch", request)
    native = toolset(f.service, f.scope, f.grant)
    selected = (await native.get_tools(f.ctx))["publish_branch"]
    arguments = selected.args_validator.validate_python({"request": request.model_dump(mode="json")})
    result = await native.call_tool("publish_branch", arguments, f.ctx, selected)
    assert result["branch"] == "work/staged"
    before = list(mutations(f.provider))
    assert await native.call_tool("publish_branch", arguments, f.ctx, selected) == result
    assert mutations(f.provider) == before
    assert calls


async def interrupt_after_ack(f, request, kind):
    """Recreate the durable state after a saved provider reply but before finish."""
    async with f.product.database.sessions.begin() as db:
        row = await db.scalar(select(ProductGitOperation).where(
            ProductGitOperation.kind == kind, ProductGitOperation.request_key == request.request_key,
        ))
        document = f.publisher.document(row)
        del document["result"]
        row.document = f.product.database.security.seal(document)
        effect = await db.get(Effect, row.original_effect_id)
        effect.state, effect.settled_at = "pending", None
        await db.execute(delete(PRPublication).where(PRPublication.id == row.original_effect_id))
    return row


async def test_status_finds_interrupted_updates_and_original_key_recovers_once(publication):
    f = publication
    pr = await owned(f)
    request = Update(repository_id=123, number=pr["number"], base_sha=pr["head"]["sha"],
                     title="Follow-up", request_key="status-update", files=[{"path": "two.txt", "content": "Two"}])
    f.ctx.tool_call_id = "status-update"
    await reserve(f, "update_pull_request", request)
    expected = await f.publisher.update(f.ctx, request)
    row = await interrupt_after_ack(f, request, "update_pull_request")
    f.current["token_unavailable"] = True
    f.provider.calls.clear()
    status = await f.publisher.status(PublicationStatus(
        repository_id=123, operation="update_pull_request", request_key=request.request_key,
    ))
    assert status["status"] == "incomplete" and "advance-branch" in status["confirmed_steps"]
    assert await f.publisher.update(f.ctx, request) == expected
    assert (await f.publisher.status(PublicationStatus(
        repository_id=123, operation="update_pull_request", request_key=request.request_key,
    )))["status"] == "confirmed"
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, row.original_effect_id)).state == "confirmed"
    assert f.provider.calls == []


@pytest.mark.parametrize("kind", [
    "create_pull_request", "open_pull_request", "create_branch", "publish_branch", "publish_branch_update",
])
@pytest.mark.parametrize("head_change", ["pushed", "deleted"])
async def test_saved_publication_ack_is_independent_of_mutable_provider_state(publication, kind, head_change):
    f = publication
    branch = "feature/recovery"
    if kind == "create_pull_request":
        request = Publish(**create_request().model_dump(), head_branch=branch)
    elif kind == "open_pull_request":
        f.provider.refs[branch] = "a" * 40
        request = OpenPullRequest(repository_id=123, request_key="open-recovery", base_branch="main",
                                  head_branch=branch, expected_head_sha="a" * 40, title="Existing head", body="Proof")
    elif kind == "create_branch":
        request = CreateBranch(repository_id=123, request_key="branch-recovery", branch=branch, commit_sha="a" * 40)
    else:
        if kind == "publish_branch_update":
            await invoke(f, "create_branch", CreateBranch(
                repository_id=123, request_key="seed-owned-branch", branch=branch, commit_sha="a" * 40,
            ))
        request = PublishBranch(repository_id=123, request_key="files-recovery", branch=branch, base_sha="a" * 40,
                                title="Prepared files", files=[{"path": "hello.txt", "content": "New"}],
                                expected_head_sha="a" * 40 if kind == "publish_branch_update" else None)
        kind = "publish_branch"
    expected = await invoke(f, kind, request)
    row = await interrupt_after_ack(f, request, kind)
    if head_change == "pushed":
        f.provider.refs[branch] = "9" * 40
    else:
        del f.provider.refs[branch]
    f.provider.default_branch = "release"
    f.provider.protected.add(branch)
    for pr in f.provider.pulls:
        pr["head"]["sha"], pr["base"]["ref"], pr["state"], pr["draft"] = "9" * 40, "release", "closed", True
    f.current["token_unavailable"] = True
    f.provider.calls.clear()
    assert await invoke(f, kind, request) == expected
    assert await invoke(f, kind, request) == expected
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, row.original_effect_id)).state == "confirmed"
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == (
            1 if kind in {"create_pull_request", "open_pull_request"} else 0
        )
    assert f.provider.calls == []


async def test_saved_branch_ack_cannot_confirm_a_different_ref(publication):
    f = publication
    request = CreateBranch(repository_id=123, branch="feature/original", commit_sha="a" * 40,
                           request_key="branch-identity")
    await invoke(f, "create_branch", request)
    row = await interrupt_after_ack(f, request, "create_branch")
    async with f.product.database.sessions.begin() as db:
        current = await db.get(ProductGitOperation, row.id)
        document = f.publisher.document(current)
        document["steps"]["branch"]["ref"] = "refs/heads/someone-else"
        current.document = f.product.database.security.seal(document)
    f.provider.calls.clear()
    with pytest.raises(ToolFailed):
        await invoke(f, "create_branch", request)
    assert not f.provider.calls
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, row.original_effect_id)).state == "pending"


@pytest.mark.parametrize("mismatch", ["head", "base", "repository", "shape", "draft"])
async def test_saved_pr_ack_must_match_its_original_frozen_identity(publication, mismatch):
    f = publication
    request = Publish(**create_request().model_dump(), head_branch="feature/verified-receipt")
    await invoke(f, "create_pull_request", request)
    row = await interrupt_after_ack(f, request, "create_pull_request")
    async with f.product.database.sessions.begin() as db:
        current = await db.get(ProductGitOperation, row.id)
        document = f.publisher.document(current)
        pr = document["steps"]["pull-request"]
        if mismatch == "head":
            pr["head"]["sha"] = "9" * 40
        elif mismatch == "base":
            pr["base"]["ref"] = "unrelated"
        elif mismatch == "repository":
            pr["head"]["repo"]["id"] = 999
        elif mismatch == "shape":
            pr["head"] = "not a reference"
        else:
            pr["draft"] = True
        current.document = f.product.database.security.seal(document)
    f.provider.calls.clear()
    with pytest.raises(ToolFailed):
        await invoke(f, "create_pull_request", request)
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, row.original_effect_id)).state == "pending"
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == 0
    assert f.provider.calls == []


async def test_hash_branch_name_round_trips_through_publication_and_update(publication):
    f = publication
    branch = "feature/fix#123"
    created = await invoke(f, "create_pull_request", Publish(
        **create_request().model_dump(), head_branch=branch,
    ))
    request = Update(repository_id=123, number=created["number"], base_sha=created["head"]["sha"],
                     title="Hash branch update", request_key="hash-branch-update",
                     files=[{"path": "hello.txt", "content": "Next"}])
    f.ctx.tool_call_id = "hash-branch-update"
    await reserve(f, "update_pull_request", request)
    updated = await f.publisher.update(f.ctx, request)
    assert updated["branch"] == branch and f.provider.refs[branch] == updated["commit"]
    assert any("%23" in path for _, path, _ in f.provider.calls)
    assert all("#" not in path for _, path, _ in f.provider.calls)


async def test_selected_base_and_head_reach_provider_and_original_key_remains_compatible(publication):
    f = publication
    request = Publish(**create_request().model_dump(), base_branch="release", head_branch="feature/chosen")
    result = await invoke(f, "create_pull_request", request)
    assert result["base"]["ref"] == "release" and result["head"]["ref"] == "feature/chosen"
    calls = len(mutations(f.provider))
    assert await f.publisher.create(f.ctx, request) == result
    assert len(mutations(f.provider)) == calls


async def test_legacy_default_request_fingerprint_is_preserved(publication):
    f = publication
    request = create_request()
    await reserve(f, "create_pull_request", request)
    row = await f.publisher.operation(f.ctx, "create_pull_request", request)
    legacy = hashlib.sha256(json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    assert row.fingerprint == legacy
    assert await f.publisher.create(f.ctx, request)


async def test_publish_then_open_creates_one_pr_receipt_and_trusted_branch_authority(publication):
    f = publication
    branch = PublishBranch(repository_id=123, request_key="publish-branch", branch="feature/prepared",
                           base_sha="a" * 40, title="Branch only", files=[{"path": "hello.txt", "content": "New"}])
    result = await invoke(f, "publish_branch", branch)
    assert not f.provider.pulls
    async with f.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == 0
    request = OpenPullRequest(repository_id=123, request_key="open-prepared", base_branch="release",
                              head_branch=branch.branch, expected_head_sha=result["commit"], title="Open prepared", body="Proof")
    pr = await invoke(f, "open_pull_request", request)
    assert pr["draft"] is False and pr["base"]["ref"] == "release"
    assert (await f.publisher.authority(pr["number"]))["head"]["ref"] == branch.branch
    async with f.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == 1
    status = await f.publisher.status(PublicationStatus(
        repository_id=123, request_key="publish-branch", operation="publish_branch"))
    assert status["result"]["commit"] == result["commit"]


@pytest.mark.parametrize("race", [False, True])
async def test_branch_only_updates_use_atomic_nonforced_expected_head(publication, race):
    f = publication
    first = PublishBranch(repository_id=123, request_key="branch-initial", branch="feature/branch-only",
                          base_sha="a" * 40, title="First branch change", files=[{"path": "one.txt", "content": "One"}])
    before = await invoke(f, "publish_branch", first)
    update = PublishBranch(repository_id=123, request_key="branch-followup", branch=first.branch,
                           base_sha=before["commit"], expected_head_sha=before["commit"],
                           title="Second branch change", files=[{"path": "two.txt", "content": "Two"}])
    f.provider.race_before_ref_update = race
    if race:
        with pytest.raises(ToolFailed, match="atomic"):
            await invoke(f, "publish_branch", update)
        assert f.provider.refs[first.branch] == "9" * 40
        calls = len(f.provider.calls)
        f.current["token_unavailable"] = True
        with pytest.raises(ToolFailed, match="not confirmed"):
            await invoke(f, "publish_branch", update)
        assert len(f.provider.calls) == calls
    else:
        result = await invoke(f, "publish_branch", update)
        assert f.provider.refs[first.branch] == result["commit"] == "f" * 40
        assert await Workflows(f.publisher).branch_owned(first.branch)
    sent = [call for call in mutations(f.provider) if call[1] == "/graphql"]
    assert len(sent) == 1 and sent[0][2]["json"]["query"] == ADVANCE
    assert sent[0][2]["json"]["variables"]["beforeOid"] == before["commit"]
    assert not f.provider.pulls


async def test_branch_only_update_does_not_adopt_an_external_branch(publication):
    f = publication
    f.provider.refs["human/work"] = "a" * 40
    request = PublishBranch(repository_id=123, request_key="external-branch", branch="human/work",
                            base_sha="a" * 40, expected_head_sha="a" * 40,
                            title="Cannot adopt branch", files=[{"path": "no.txt", "content": "No"}])
    with pytest.raises(ToolFailed, match="ownership"):
        await invoke(f, "publish_branch", request)
    assert not mutations(f.provider)


async def test_opening_external_branch_never_grants_branch_write_ownership(publication):
    f = publication
    f.provider.refs["human/work"] = "a" * 40
    request = OpenPullRequest(repository_id=123, request_key="open-external", base_branch="main",
                              head_branch="human/work", expected_head_sha="a" * 40, title="Existing work", body="Open only")
    pr = await invoke(f, "open_pull_request", request)
    assert not await Workflows(f.publisher).branch_owned("human/work")
    assert await f.publisher.authority(pr["number"], branch_write=False)
    f.ctx.tool_call_id = "update-external"
    update = Update(repository_id=123, number=pr["number"], base_sha="a" * 40, request_key="external-update",
                    title="Cannot modify", files=[{"path": "hello.txt", "content": "Forbidden"}])
    await reserve(f, "update_pull_request", update)
    f.provider.calls.clear()
    with pytest.raises(ToolFailed, match="approval"):
        await f.publisher.update(f.ctx, update)
    assert not mutations(f.provider)


@pytest.mark.parametrize("comparison", ["ahead", "diverged"])
async def test_recreated_branch_with_unacknowledged_head_loses_write_ownership(publication, comparison):
    f = publication
    await invoke(f, "create_branch", CreateBranch(
        repository_id=123, branch="feature/replaced", commit_sha="a" * 40, request_key="created-once"))
    f.provider.refs["feature/replaced"] = "9" * 40
    f.provider.compare_status = comparison
    assert not await Workflows(f.publisher).branch_owned("feature/replaced")
    pr = await invoke(f, "open_pull_request", OpenPullRequest(
        repository_id=123, head_branch="feature/replaced", expected_head_sha="9" * 40,
        base_branch="main", title="External replacement", body="No branch ownership", request_key="open-replacement"))
    with pytest.raises(ToolFailed, match="approval"):
        await f.publisher.authority(pr["number"], base_sha="9" * 40)


@pytest.mark.parametrize("branch", ["main", "protected", "already-exists"])
async def test_branch_creation_refuses_default_protected_and_existing_before_writes(publication, branch):
    f = publication
    if branch != "main":
        f.provider.refs[branch] = "a" * 40
    f.provider.protected.add("protected")
    request = CreateBranch(repository_id=123, branch=branch, commit_sha="a" * 40, request_key="branch-refusal")
    with pytest.raises(ToolFailed):
        await invoke(f, "create_branch", request)
    assert not mutations(f.provider)


async def test_deleted_acknowledged_branch_is_not_recreated(publication):
    f = publication
    request = CreateBranch(repository_id=123, branch="feature/deleted", commit_sha="a" * 40, request_key="delete-ref")
    expected = await invoke(f, "create_branch", request)
    async with f.product.database.sessions.begin() as db:
        row = await db.scalar(select(ProductGitOperation).where(ProductGitOperation.kind == "create_branch"))
        document = f.publisher.document(row)
        del document["result"]
        row.document = f.product.database.security.seal(document)
    del f.provider.refs[request.branch]
    f.provider.calls.clear()
    assert await Workflows(f.publisher).create_branch(f.ctx, request) == expected
    assert request.branch not in f.provider.refs
    assert not mutations(f.provider)


async def test_deleted_intermediate_branch_cannot_be_recreated_to_finish_a_pr(publication):
    from app.connector_errors import ConnectorError
    f = publication
    request = Publish(**create_request().model_dump(), head_branch="feature/intermediate")
    f.provider.fail_before_pr = True
    with pytest.raises(ConnectorError, match="before PR"):
        await invoke(f, "create_pull_request", request)
    assert not f.provider.pulls
    del f.provider.refs[request.head_branch]
    f.provider.fail_before_pr = False
    f.provider.calls.clear()
    with pytest.raises(ToolFailed, match="deleted"):
        await invoke(f, "create_pull_request", request)
    assert not mutations(f.provider) and not f.provider.pulls


async def test_concurrent_brokers_claim_each_branch_effect_only_once(publication):
    f = publication
    request = CreateBranch(repository_id=123, branch="feature/concurrent", commit_sha="a" * 40, request_key="two-brokers")
    f.ctx.tool_call_id = "concurrent"
    await reserve(f, "create_branch", request)
    # Both callers have the same durable effect; the row lock, not a local mutex, is the fence.
    result = await asyncio.gather(
        Workflows(f.publisher).create_branch(f.ctx, request),
        Workflows(Publications(f.service, f.scope, f.grant)).create_branch(f.ctx, request),
        return_exceptions=True,
    )
    assert any(isinstance(value, dict) for value in result)
    assert len([call for call in mutations(f.provider) if call[1].endswith("/git/refs")]) == 1


@pytest.mark.parametrize("files", [[], [{"path": "hello.txt", "content": "Rebased"}]])
async def test_rebase_is_atomic_and_recovers_saved_ack_without_repeating_mutation(publication, files):
    f = publication
    pr = await owned(f)
    request = Rebase(repository_id=123, number=pr["number"], expected_head_sha=pr["head"]["sha"],
                     base_branch="main", base_sha="a" * 40, request_key="atomic-rebase",
                     title="Prepared rebase", files=files)
    result = await invoke(f, "rebase_pull_request", request)
    calls = [call for call in mutations(f.provider) if call[1] == "/graphql"]
    assert len(calls) == 1 and calls[0][2]["json"]["query"] == REBASE
    assert calls[0][2]["json"]["variables"]["beforeOid"] == "e" * 40
    assert result["commit"] == "f" * 40 and f.provider.refs[pr["head"]["ref"]] == result["commit"]
    async with f.product.database.sessions.begin() as db:
        row = await db.scalar(select(ProductGitOperation).where(ProductGitOperation.kind == "rebase_pull_request"))
        document = f.publisher.document(row)
        del document["result"]
        row.document = f.product.database.security.seal(document)
    f.provider.refs[pr["head"]["ref"]] = "9" * 40
    f.provider.pulls[0]["head"]["sha"] = "9" * 40
    assert await Workflows(f.publisher).rebase(f.ctx, request) == result
    assert len([call for call in mutations(f.provider) if call[1] == "/graphql"]) == 1


async def test_stale_rebase_head_is_refused_before_commit_writes(publication):
    f = publication
    pr = await owned(f)
    f.provider.pulls[0]["head"]["sha"] = "9" * 40
    f.provider.calls.clear()
    request = Rebase(repository_id=123, number=pr["number"], expected_head_sha="e" * 40,
                     base_branch="main", base_sha="a" * 40, request_key="stale-rebase",
                     title="Prepared rebase", files=[{"path": "hello.txt", "content": "Rebased"}])
    with pytest.raises(ToolFailed):
        await invoke(f, "rebase_pull_request", request)
    assert not mutations(f.provider)


async def test_retarget_preserves_creation_receipt_and_updates_owned_authority(publication):
    f = publication
    pr = await owned(f)
    request = Retarget(repository_id=123, number=pr["number"], head_sha=pr["head"]["sha"],
                       base_branch="release", request_key="retarget-release")
    result = await invoke(f, "retarget_pull_request", request)
    assert result["base"]["ref"] == "release"
    assert (await f.publisher.authority(pr["number"]))["base"]["ref"] == "release"
    async with f.product.database.sessions() as db:
        row = await db.scalar(select(ProductGitOperation).where(ProductGitOperation.kind == "create_pull_request"))
        assert f.publisher.document(row)["result"]["base"]["ref"] == "main"
    f.provider.sources["develop"] = "a" * 40
    await invoke(f, "retarget_pull_request", request.model_copy(update={
        "base_branch": "develop", "request_key": "second-retarget",
    }))
    assert (await f.publisher.authority(pr["number"]))["base"]["ref"] == "develop"


async def test_close_reopen_and_explicit_draft_conversion_keep_same_pr(publication):
    f = publication
    pr = await owned(f)
    for operation, expected in [("close_pull_request", "closed"), ("reopen_pull_request", "open")]:
        result = await invoke(f, operation, PRWrite(
            repository_id=123, number=pr["number"], head_sha=pr["head"]["sha"], request_key=operation))
        assert result["number"] == pr["number"] and result["state"] == expected
    result = await invoke(f, "convert_pull_request_to_draft", PRWrite(
        repository_id=123, number=pr["number"], head_sha=pr["head"]["sha"], request_key="explicit-draft"))
    assert result["data"]["convertPullRequestToDraft"]["pullRequest"]["isDraft"]


async def test_pending_review_ownership_is_bound_to_current_task_and_head(publication):
    f = publication
    pr = await owned(f)
    args = {"repository_id": 123, "number": pr["number"], "head_sha": pr["head"]["sha"]}
    review = await invoke(f, "create_review", CreateReview(
        **args, request_key="pending-review", body="Review notes",
        comments=[{"path": "hello.txt", "line": 1, "body": "Inline note"}],
    ))
    emitted = next(call for call in mutations(f.provider) if call[1].endswith("/reviews"))
    assert emitted[2]["json"]["comments"][0]["body"] == "Inline note"
    pending = PendingReview(**args, review_id=review["id"], request_key="pending-action")
    later = Workflows(Publications(f.service, replace(f.scope, turn_id="different-task"), f.grant))
    with pytest.raises(ToolFailed, match="ownership"):
        await later.pending_review(pending)
    edited = await invoke(f, "edit_pending_review", EditReview(
        **pending.model_dump(exclude={"request_key"}), request_key="edit-pending", body="Updated notes"))
    assert edited["body"] == "Updated notes"
    submitted = await invoke(f, "submit_review", SubmitReview(
        **pending.model_dump(exclude={"request_key"}), request_key="submit-pending", event="COMMENT", body="Final notes"))
    assert submitted["state"] == "COMMENTED"
    with pytest.raises(ToolFailed, match="pending review"):
        await invoke(f, "delete_pending_review", pending)


async def test_closed_external_pr_has_separate_task_lifecycle_consent(publication):
    f = publication
    pr = {
        "number": 81, "node_id": "PR_81", "html_url": "https://github.com/owner/repo/pull/81",
        "state": "closed", "draft": False, "merged": False, "merged_at": None,
        "head": {"ref": "human/branch", "sha": "a" * 40, "repo": {"id": 123}},
        "base": {"ref": "main", "sha": "a" * 40, "repo": {"id": 123}},
    }
    f.provider.pulls.append(pr)
    consent = LifecycleAccess(repository_id=123, number=81, head_sha="a" * 40, request_key="lifecycle-consent")
    f.ctx.tool_call_id = "consent"
    await reserve(f, "request_pr_lifecycle_access", consent)
    with pytest.raises(ToolFailed, match="explicit"):
        await f.publisher.approve_lifecycle(f.ctx, consent)
    f.ctx.tool_call_approved = True
    assert (await f.publisher.approve_lifecycle(f.ctx, consent))["scope"] == "task_pr_lifecycle"
    with pytest.raises(ToolFailed, match="approval"):
        await f.publisher.authority(81, base_sha="a" * 40)
    # Lifecycle consent covers the PR, while content consent remains pinned to an exact head.
    pr["head"]["sha"] = "b" * 40
    result = await invoke(f, "reopen_pull_request", PRWrite(
        repository_id=123, number=81, head_sha="b" * 40, request_key="reopen-consented"))
    assert result["state"] == "open"
    later = Publications(f.service, replace(f.scope, turn_id="later-task"), f.grant)
    with pytest.raises(ToolFailed, match="lifecycle"):
        await later.authority(81, branch_write=False, lifecycle=True)


@pytest.mark.parametrize("kind", ["content", "lifecycle"])
async def test_generated_child_cannot_grant_external_pr_authority(publication, kind):
    f = publication
    publisher = Publications(f.service, replace(f.scope, delegation_generated=True), f.grant)
    f.ctx.tool_call_approved = True
    values = {"repository_id": 123, "number": 81, "head_sha": "a" * 40, "request_key": "child-consent"}
    with pytest.raises(ToolFailed, match="explicit"):
        if kind == "lifecycle":
            await publisher.approve_lifecycle(f.ctx, LifecycleAccess(**values))
        else:
            await publisher.approve(f.ctx, PRWrite(**values))
    assert not f.provider.calls


async def test_revoked_lifecycle_grant_blocks_the_durable_dispatch_claim(publication):
    f = publication
    pr = {
        "number": 81, "node_id": "PR_81", "html_url": "https://github.com/owner/repo/pull/81",
        "state": "open", "draft": False, "merged": False,
        "head": {"ref": "human/branch", "sha": "a" * 40, "repo": {"id": 123}},
        "base": {"ref": "main", "sha": "a" * 40, "repo": {"id": 123}},
    }
    f.provider.pulls.append(pr)
    consent = LifecycleAccess(repository_id=123, number=81, head_sha="a" * 40, request_key="consent-before-revoke")
    f.ctx.tool_call_id, f.ctx.tool_call_approved = "consent-revoke", True
    await reserve(f, "request_pr_lifecycle_access", consent)
    await f.publisher.approve_lifecycle(f.ctx, consent)
    authority = await f.publisher.authority(81, branch_write=False, lifecycle=True)
    request = PRWrite(repository_id=123, number=81, head_sha="a" * 40, request_key="close-after-revoke")
    f.ctx.tool_call_id = "close-after-revoke"
    await reserve(f, "close_pull_request", request)
    row = await f.publisher.operation(f.ctx, "close_pull_request", request)
    await f.publisher.bind_authority(
        row, AuthorityRef.model_validate(authority), number=81, head="a" * 40, base="main", lifecycle=True,
    )
    await Workflows(f.publisher).retarget_authority(81, "main", "release")
    f.provider.calls.clear()
    with pytest.raises(ToolFailed, match="revoked"):
        await f.publisher.step(row, "pull-request", "PATCH", "/repositories/123/pulls/81",
                              "synthetic-token", {"state": "closed"})
    assert not mutations(f.provider)


async def test_retarget_ack_recovers_after_a_concurrent_push(publication):
    f = publication
    pr = await owned(f)
    f.provider.patch_head = "9" * 40
    request = Retarget(repository_id=123, number=pr["number"], head_sha=pr["head"]["sha"],
                       base_branch="release", request_key="retarget-with-push")
    result = await invoke(f, "retarget_pull_request", request)
    assert result["head"]["sha"] == "9" * 40
    async with f.product.database.sessions.begin() as db:
        row = await db.scalar(select(ProductGitOperation).where(ProductGitOperation.kind == "retarget_pull_request"))
        document = f.publisher.document(row)
        del document["result"]
        row.document = f.product.database.security.seal(document)
    count = len(mutations(f.provider))
    assert await invoke(f, "retarget_pull_request", request) == result
    assert len(mutations(f.provider)) == count


async def test_old_retarget_recovery_cannot_reapply_projection_after_a_newer_round_trip(publication):
    f = publication
    pr = await owned(f)
    request = Retarget(repository_id=123, number=pr["number"], head_sha=pr["head"]["sha"],
                       base_branch="release", request_key="first-retarget")
    original = await invoke(f, "retarget_pull_request", request)
    async with f.product.database.sessions.begin() as db:
        row = await db.scalar(select(ProductGitOperation).where(
            ProductGitOperation.kind == "retarget_pull_request",
            ProductGitOperation.request_key == request.request_key,
        ))
        document = f.publisher.document(row)
        assert document["authority_projection_applied"]
        del document["result"]
        row.document = f.product.database.security.seal(document)
    await invoke(f, "retarget_pull_request", request.model_copy(update={
        "base_branch": "main", "request_key": "newer-retarget",
    }))
    assert await invoke(f, "retarget_pull_request", request) == original
    assert f.provider.pulls[0]["base"]["ref"] == "main"
    assert (await f.publisher.authority(pr["number"]))["base"]["ref"] == "main"


@pytest.mark.parametrize("kind", ["close_pull_request", "submit_review", "delete_pending_review"])
async def test_acknowledged_actions_finish_without_repeating_or_requiring_old_state(publication, kind):
    f = publication
    pr = await owned(f)
    args = {"repository_id": 123, "number": pr["number"], "head_sha": pr["head"]["sha"]}
    if kind == "close_pull_request":
        request = PRWrite(**args, request_key="interrupted-action")
    else:
        pending = await invoke(f, "create_review", CreateReview(**args, request_key="owned-pending", body="Notes"))
        model = SubmitReview if kind == "submit_review" else PendingReview
        request = model(**args, request_key="interrupted-action", review_id=pending["id"],
                        **({"event": "COMMENT"} if kind == "submit_review" else {}))
    result = await invoke(f, kind, request)
    async with f.product.database.sessions.begin() as db:
        row = await db.scalar(select(ProductGitOperation).where(ProductGitOperation.kind == kind))
        document = f.publisher.document(row)
        del document["result"]
        row.document = f.product.database.security.seal(document)
    count = len(mutations(f.provider))
    assert await invoke(f, kind, request) == result
    assert len(mutations(f.provider)) == count


async def test_stack_create_add_and_ack_recovery_authorize_every_member(publication):
    f = publication
    first = await owned(f)
    second = await invoke(f, "create_pull_request", Publish(
        repository_id=123, base_sha=first["head"]["sha"], base_branch=first["head"]["ref"],
        head_branch="stack/second", title="Second change", body="Stack upper change",
        request_key="stack-second", files=[{"path": "second.txt", "content": "Two"}]))
    request = CreateStack(repository_id=123, request_key="stack-create",
                          pull_requests=[StackMember.model_validate(value) for value in [first, second]])
    f.ctx.tool_call_id = "stack-create"
    await reserve(f, "create_stack", request)
    result = await Stacks(f.publisher).write(f.ctx, "create_stack", request)
    assert result["outcome"] == "created" and result["remaining_prs"] == [7, 8]
    read = await Stacks(f.publisher).read(StackRead(repository_id=123, stack_number=1))
    third = await invoke(f, "create_pull_request", Publish(
        repository_id=123, base_sha=second["head"]["sha"], base_branch=second["head"]["ref"],
        head_branch="stack/third", title="Third change", body="Stack top change",
        request_key="stack-third", files=[{"path": "third.txt", "content": "Three"}]))
    append = AddToStack(repository_id=123, request_key="stack-append", stack_number=1,
                        revision=read["revision"], members=read["members"],
                        pull_requests=[StackMember.model_validate(third)])
    f.ctx.tool_call_id = "stack-add"
    await reserve(f, "add_to_stack", append)
    result = await Stacks(f.publisher).write(f.ctx, "add_to_stack", append)
    assert result["remaining_prs"] == [7, 8, 9]
    async with f.product.database.sessions.begin() as db:
        row = await db.scalar(select(ProductGitOperation).where(ProductGitOperation.kind == "add_to_stack"))
        document = f.publisher.document(row)
        del document["result"]
        row.document = f.product.database.security.seal(document)
    count = len(mutations(f.provider))
    assert await Stacks(f.publisher).write(f.ctx, "add_to_stack", append) == result
    assert len(mutations(f.provider)) == count


@pytest.mark.parametrize("operation", [
    ("PUT", "/repositories/123/pulls/7/merge", {}),
    ("POST", "/repositories/123/pulls/7/reviews", {"event": "APPROVE"}),
    ("POST", "/graphql", {"query": "mutation { mergePullRequest(input: {}) { clientMutationId } }", "variables": {}}),
    ("PATCH", "/repositories/123/git/refs/heads/feature", {"force": True, "sha": "a" * 40}),
    ("POST", "/repos/owner/repo/stacks/1/unstack", {}),
])
async def test_forbidden_actions_never_reach_transport(publication, operation):
    f = publication
    method, path, body = operation
    with pytest.raises(ToolFailed):
        await f.publisher.request(method, path, "synthetic-token", json=body)
    assert f.provider.calls == []


@pytest.mark.parametrize("arguments", [
    {"directory": "/workspace/../escape"}, {"directory": "/workspace/.git"},
    {"directory": "/workspace/repo", "ref": "main~1"},
    {"directory": "/workspace/repo", "ref": "main", "number": 7},
])
async def test_checkout_rejects_ref_expressions_and_path_traversal(arguments):
    with pytest.raises(ValidationError):
        Checkout(**arguments)
