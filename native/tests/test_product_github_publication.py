"""Real local SQL publication receipts with a deterministic provider protocol fixture."""

import asyncio
import copy
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_authority as authority
from app.connector_errors import ConnectorError
from app.github import GitHub, Publish, Update
from app.github_coding import PRWrite
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from sqlalchemy import func, select

from moyai.contracts import Grant, NewMessage, NewSession
from moyai.effects import EffectReceipts, effect_id
from moyai.models import Effect
from moyai.pr_publications import PRPublication
from moyai.product_connector_execution import CanonicalConnectorExecution
from moyai.product_github_publication import Publications, toolset

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
product_database = authority.product_database
admitted_product = authority.admitted_product


class Provider:
    def __init__(self):
        self.calls, self.refs, self.pulls = [], {}, []
        self.sources = {"main": "a" * 40, "release": "a" * 40}
        self.default_branch = "main"
        self.protected = set()
        self.reviews = {}
        self.stacks = {}
        self.compare_status = "ahead"
        self.patch_head = None
        self.race_before_ref_update = False
        self.lost_create = False
        self.fail_before_pr = False
        self.visible = True
        self.symlink = False
        self.commits = 0

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        prefix = "/repositories/123"
        if method == "GET" and path == prefix:
            return {"id": 123, "node_id": "R_123", "full_name": "owner/repo", "default_branch": self.default_branch}
        if method == "GET" and path.startswith(prefix + "/commits/"):
            from urllib.parse import unquote
            ref = unquote(path.removeprefix(prefix + "/commits/"))
            return {"sha": self.sources.get(ref, self.refs.get(ref, ref))}
        if method == "GET" and "/compare/" in path:
            return {"status": self.compare_status}
        if method == "GET" and "/rules/branches/" in path:
            return []
        if method == "GET" and path.startswith(prefix + "/branches/"):
            from urllib.parse import unquote
            branch = unquote(path.removeprefix(prefix + "/branches/"))
            return {"name": branch, "protected": branch in self.protected} if branch in self.refs else None
        if method == "POST" and path == "/graphql":
            from moyai.github_request_policy import ADVANCE, DRAFT, PROTECTION, REBASE
            query, variables = kwargs["json"]["query"], kwargs["json"]["variables"]
            if query == PROTECTION:
                return {"data": {"repository": {"databaseId": 123, "branchProtectionRules": {
                    "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}}}
            if query in {REBASE, ADVANCE}:
                branch = variables["name"].removeprefix("refs/heads/")
                if self.race_before_ref_update:
                    self.refs[branch] = "9" * 40
                if self.refs[branch] != variables["beforeOid"]:
                    return {"errors": [{"message": "stale head"}]}
                self.refs[branch] = variables["afterOid"]
                for pr in self.pulls:
                    if pr["head"]["ref"] == branch:
                        pr["head"]["sha"] = variables["afterOid"]
                return {"data": {"updateRefs": {"clientMutationId": None}}}
            if query == DRAFT:
                pr = next(pr for pr in self.pulls if pr["node_id"] == variables["id"])
                pr["draft"] = True
                return {"data": {"convertPullRequestToDraft": {
                    "pullRequest": {"number": pr["number"], "isDraft": True, "url": pr["html_url"]},
                }}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "b" * 40}}
        if method == "GET" and "/git/trees/" in path:
            return {"tree": [{"path": "hello.txt", "sha": "c" * 40, "type": "blob", "mode": "120000"}] if self.symlink else []}
        if method == "POST" and path.endswith("/git/blobs"):
            return {"sha": "c" * 40}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": "d" * 40}
        if method == "POST" and path.endswith("/git/commits"):
            self.commits += 1
            return {"sha": "ef123456789abcd"[(self.commits - 1) % 15] * 40}
        if method == "GET" and "/git/ref/heads/" in path:
            from urllib.parse import unquote
            branch = unquote(path.split("/git/ref/heads/", 1)[1])
            sha = self.refs.get(branch)
            return {"ref": "refs/heads/" + branch, "object": {"type": "commit", "sha": sha}} if sha else None
        if method == "POST" and path.endswith("/git/refs"):
            body = kwargs["json"]
            self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return {"ref": body["ref"], "object": {"type": "commit", "sha": body["sha"]}}
        if method == "PATCH" and "/git/refs/heads/" in path:
            from urllib.parse import unquote
            branch, body = unquote(path.split("/git/refs/heads/", 1)[1]), kwargs["json"]
            assert body["force"] is False
            self.refs[branch] = body["sha"]
            for pr in self.pulls:
                if pr["head"]["ref"] == branch:
                    pr["head"]["sha"] = body["sha"]
            return {"ref": "refs/heads/" + branch, "object": {"type": "commit", "sha": body["sha"]}}
        if method == "GET" and path == prefix + "/pulls":
            if self.fail_before_pr:
                raise ConnectorError("Interrupted before PR creation.")
            params = kwargs.get("params", {})
            return copy.deepcopy([pr for pr in self.pulls if (
                ("head" not in params or params["head"] == "owner:" + pr["head"]["ref"])
                and ("base" not in params or params["base"] == pr["base"]["ref"])
            )]) if self.visible else []
        if path.startswith("/repos/owner/repo/stacks"):
            stack_prefix = "/repos/owner/repo/stacks"
            if method == "GET":
                if path != stack_prefix:
                    return copy.deepcopy(self.stacks[int(path.rsplit("/", 1)[1])])
                selected = kwargs.get("params", {}).get("pull_request")
                return copy.deepcopy([stack for stack in self.stacks.values() if (
                    selected is None or selected in {pr["number"] for pr in stack["pull_requests"]}
                )])
            additions = [copy.deepcopy(next(pr for pr in self.pulls if pr["number"] == number))
                         for number in kwargs["json"]["pull_requests"]]
            if path == stack_prefix:
                number = len(self.stacks) + 1
                stack = {"id": 500 + number, "number": number, "node_id": f"ST_{number}", "open": True,
                         "base": {"ref": additions[0]["base"]["ref"]}, "pull_requests": additions}
                self.stacks[number] = stack
                return {"status_code": 201, "data": copy.deepcopy(stack)}
            stack = self.stacks[int(path.split("/stacks/")[1].split("/")[0])]
            stack["pull_requests"].extend(additions)
            return {"status_code": 200, "data": copy.deepcopy(stack)}
        if "/reviews" in path:
            number = int(path.split("/pulls/")[1].split("/")[0])
            if method == "POST" and path.endswith("/reviews"):
                body = kwargs["json"]
                review = {"id": len(self.reviews) + 1, "commit_id": body["commit_id"], "body": body["body"],
                          "state": {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(
                              body.get("event"), "PENDING")}
                self.reviews[review["id"]] = review
                return copy.deepcopy(review)
            identity = int(path.split("/reviews/")[1].split("/")[0])
            review = self.reviews[identity]
            if method in {"POST", "PUT"}:
                review["body"] = kwargs["json"]["body"]
                if method == "POST":
                    review["state"] = {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[
                        kwargs["json"]["event"]]
            if method == "DELETE":
                del self.reviews[identity]
            return copy.deepcopy(review)
        if method in {"GET", "PATCH"} and path.startswith(prefix + "/pulls/"):
            if method == "PATCH":
                pr = next(pr for pr in self.pulls if pr["number"] == int(path.rsplit("/", 1)[1]))
                body = kwargs["json"]
                if "state" in body:
                    pr["state"] = body["state"]
                if "base" in body:
                    pr["base"]["ref"] = body["base"]
                if self.patch_head is not None:
                    pr["head"]["sha"] = self.patch_head
            return copy.deepcopy(next(pr for pr in self.pulls if pr["number"] == int(path.rsplit("/", 1)[1])))
        if method == "POST" and path == prefix + "/pulls":
            body = kwargs["json"]
            assert body["draft"] is False
            number = len(self.pulls) + 7
            pr = {
                "id": 1000 + number, "number": number, "node_id": f"PR_{number}",
                "html_url": f"https://github.com/owner/repo/pull/{number}",
                "created_at": "2026-10-15T12:00:00Z", "state": "open", "draft": False, "merged": False,
                "merged_at": None,
                "head": {"ref": body["head"], "sha": self.refs[body["head"]], "repo": {"id": 123}},
                "base": {"ref": body["base"], "sha": self.sources.get(body["base"], self.refs.get(body["base"])),
                         "repo": {"id": 123, "full_name": "owner/repo"}},
            }
            self.pulls.append(pr)
            if self.lost_create:
                raise ConnectorError("Synthetic lost provider acknowledgement.")
            return copy.deepcopy(pr)
        raise AssertionError((method, path))


@pytest_asyncio.fixture
async def publication(admitted_product):
    product = admitted_product
    grant = Grant("11111111-1111-1111-1111-111111111111", "github", 1, "", read_only=False,
                  policy={"product": {"repository_id": 123}})
    current = {"allowed": True}
    async def resolve(actor_id, selections, **kwargs):
        if not current["allowed"]:
            raise HTTPException(403, "Revoked")
        return [grant] if selections else []
    product.database.product_connections = SimpleNamespace(resolve_grants=resolve)
    session = await product.database.create_session(product.actor, NewSession())
    turn = await product.database.submit(product.actor, session.id, NewMessage(prompt="Local protocol test", request_key="publication", connections=[grant.id]))
    await product.database.claim_next()
    prepared = await product.database.prepare(turn.id)
    provider = Provider()
    async def check(scope, selected):
        assert selected == grant
        await product.database.authorize_scope(scope)
    async def credentials(scope, selected):
        await check(scope, selected)
        return {"access_token": "synthetic-token"}
    class SourceGit:
        validate_tree_paths = GitHub.validate_tree_paths
        def target(self, value): return 123 if value in {123, "owner/repo"} else 999
        def connection_version(self): return "current-canonical-version"
        async def request(self, *args, **kwargs): return await provider.request(*args, **kwargs)
        async def installation_token(self, **kwargs):
            if current.get("token_unavailable"):
                raise ConnectorError("The provider token is currently unavailable.")
            return "synthetic-token"
        def saved_credentials(self): return {}
    service = SimpleNamespace(
        database=product.database, product=SimpleNamespace(identity=product.adapter),
        connectors=SimpleNamespace(
            github=SourceGit(), personal_slack=SimpleNamespace(),
            locks={"github": asyncio.Lock()}, request=provider.request,
        ), check=check, credentials=credentials,
        store=SimpleNamespace(rows=lambda *_args: []),
    )
    service.execution = CanonicalConnectorExecution(service)
    ctx = RunContext(deps=prepared.scope, model=TestModel(), usage=RunUsage(),
                     run_id=turn.id, tool_call_id="create")
    return SimpleNamespace(product=product, service=service, scope=prepared.scope, ctx=ctx, grant=grant,
                           publisher=Publications(service, prepared.scope, grant), provider=provider, current=current)


def create_request():
    return Publish(repository_id=123, base_sha="a" * 40, request_key="create-protocol",
                   title="Scoped publication", body="Synthetic local provider test.",
                   files=[{"path": "hello.txt", "content": "Updated"}])


async def reserve(f, kind, request):
    name = f"github_{f.grant.id[:8]}_{kind}"
    identity = effect_id(f.ctx, ToolCallPart(name, {}, tool_call_id=f.ctx.tool_call_id))
    await EffectReceipts(f.product.database).reserve(f.scope, identity, name, {"request": request.model_dump(mode="json")})
    return identity


async def test_regular_publication_and_update_use_receipts_and_never_force(publication):
    f = publication
    request = create_request()
    effect = await reserve(f, "create_pull_request", request)
    result = await f.publisher.create(f.ctx, request)
    assert result["draft"] is False and result["number"] == 7
    assert await f.publisher.create(f.ctx, request) == result
    assert len([item for item in f.provider.calls if item[0] == "POST" and item[1].endswith("/pulls")]) == 1
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, effect)).state == "confirmed"
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == 1
    f.ctx.tool_call_id = "followup"
    update = Update(repository_id=123, number=7, base_sha="e" * 40, request_key="follow-up-once",
                    title="Follow-up", files=[{"path": "hello.txt", "content": "Next"}])
    await reserve(f, "update_pull_request", update)
    changed = await f.publisher.update(f.ctx, update)
    assert changed["commit"] == "f" * 40
    patches = [args["json"] for method, _path, args in f.provider.calls if method == "PATCH"]
    assert patches == [{"sha": "f" * 40, "force": False}]


async def test_uncertain_creation_does_not_invent_authorship_from_readback(publication):
    f = publication
    f.provider.lost_create = True
    request = create_request()
    await reserve(f, "create_pull_request", request)
    with pytest.raises(ToolFailed):
        await f.publisher.create(f.ctx, request)
    with pytest.raises(ToolFailed, match="acknowledgment"):
        await f.publisher.create(f.ctx, request)
    assert len([item for item in f.provider.calls if item[0] == "POST" and item[1].endswith("/pulls")]) == 1


async def test_unconfirmed_invisible_creation_retains_write_barrier(publication):
    f = publication
    f.provider.lost_create, f.provider.visible = True, False
    request = create_request()
    identity = await reserve(f, "create_pull_request", request)
    with pytest.raises(ToolFailed):
        await f.publisher.create(f.ctx, request)
    with pytest.raises(ToolFailed, match="will not be repeated"):
        await f.publisher.create(f.ctx, request)
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, identity)).state == "pending"
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == 0
    assert len([item for item in f.provider.calls if item[0] == "POST" and item[1].endswith("/pulls")]) == 1


async def test_symlink_and_protected_paths_refuse_publication(publication):
    f = publication
    f.provider.symlink = True
    request = create_request()
    await reserve(f, "create_pull_request", request)
    with pytest.raises(ToolFailed, match="regular files"):
        await f.publisher.create(f.ctx, request)
    from moyai.github_request_policy import PROTECTION
    assert not [item for item in f.provider.calls if item[0] != "GET"
                and item[2].get("json", {}).get("query") != PROTECTION]
    with pytest.raises(ValidationError):
        Publish.model_validate({**request.model_dump(), "files": [{"path": ".env", "content": "No export"}]})


async def test_changed_pr_head_refuses_update_before_any_write(publication):
    f = publication
    request = create_request()
    await reserve(f, "create_pull_request", request)
    await f.publisher.create(f.ctx, request)
    f.provider.pulls[0]["head"]["sha"] = "9" * 40
    f.ctx.tool_call_id = "stale-followup"
    update = Update(repository_id=123, number=7, base_sha="e" * 40, request_key="stale-followup",
                    title="Stale", files=[{"path": "hello.txt", "content": "Must not write"}])
    await reserve(f, "update_pull_request", update)
    f.provider.calls.clear()
    with pytest.raises(ToolFailed, match="head changed"):
        await f.publisher.update(f.ctx, update)
    assert all(method == "GET" for method, _, _ in f.provider.calls)


async def test_revoked_selected_grant_blocks_publication_before_provider_dispatch(publication):
    f = publication
    request = create_request()
    await reserve(f, "create_pull_request", request)
    f.current["allowed"] = False
    with pytest.raises(HTTPException):
        await f.publisher.create(f.ctx, request)
    assert not f.provider.calls


async def test_external_pr_access_requires_sdk_approval_and_current_head(publication):
    f = publication
    tools = toolset(f.service, f.scope, f.grant)
    definitions = await tools.get_tools(f.ctx)
    assert definitions["request_pull_request_write_access"].tool_def.kind == "unapproved"
    request = PRWrite(repository_id=123, number=8, head_sha="a" * 40, request_key="external-pr")
    with pytest.raises(ToolFailed, match="explicit native approval"):
        await f.publisher.approve(f.ctx, request)
    assert not f.provider.calls
    f.provider.pulls.append({
        "number": 8, "html_url": "https://github.com/owner/repo/pull/8", "state": "open", "draft": False,
        "head": {"ref": "external", "sha": "b" * 40, "repo": {"id": 123}},
        "base": {"ref": "main", "repo": {"id": 123}},
    })
    f.ctx.tool_call_approved = True
    await reserve(f, "request_pull_request_write_access", request)
    with pytest.raises(ToolFailed, match="head changed"):
        await f.publisher.approve(f.ctx, request)
    f.provider.pulls[0]["head"]["sha"] = "a" * 40
    result = await f.publisher.approve(f.ctx, request)
    assert result["status"] == "approved"
    assert (await f.publisher.authority(8, base_sha="a" * 40))["head"]["ref"] == "external"
    with pytest.raises(ToolFailed, match="current task"):
        await f.publisher.authority(8, base_sha="b" * 40)
    later = Publications(f.service, replace(f.scope, turn_id="another-task"), f.grant)
    with pytest.raises(ToolFailed, match="current task"):
        await later.authority(8, base_sha="a" * 40)
