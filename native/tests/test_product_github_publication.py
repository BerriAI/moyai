"""Real local SQL publication receipts with a deterministic provider protocol fixture."""

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
from moyai.product_github_publication import Publications, toolset

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
product_database = authority.product_database
admitted_product = authority.admitted_product


class Provider:
    def __init__(self):
        self.calls, self.refs, self.pulls = [], {}, []
        self.lost_create = False
        self.visible = True
        self.symlink = False
        self.commits = 0

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        prefix = "/repositories/123"
        if method == "GET" and path == prefix:
            return {"id": 123, "full_name": "owner/repo", "default_branch": "main"}
        if method == "GET" and path == prefix + "/commits/main":
            return {"sha": "a" * 40}
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
            return {"sha": ("e" if self.commits == 1 else "f") * 40}
        if method == "GET" and "/git/ref/heads/" in path:
            sha = self.refs.get(path.split("/git/ref/heads/", 1)[1])
            return {"object": {"sha": sha}} if sha else None
        if method == "POST" and path.endswith("/git/refs"):
            body = kwargs["json"]
            self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return {"object": {"sha": body["sha"]}}
        if method == "PATCH" and "/git/refs/heads/" in path:
            branch, body = path.split("/git/refs/heads/", 1)[1], kwargs["json"]
            assert body["force"] is False
            self.refs[branch] = body["sha"]
            for pr in self.pulls:
                if pr["head"]["ref"] == branch:
                    pr["head"]["sha"] = body["sha"]
            return {"object": {"sha": body["sha"]}}
        if method == "GET" and path == prefix + "/pulls":
            return copy.deepcopy(self.pulls) if self.visible else []
        if method == "GET" and path.startswith(prefix + "/pulls/"):
            return copy.deepcopy(next(pr for pr in self.pulls if pr["number"] == int(path.rsplit("/", 1)[1])))
        if method == "POST" and path == prefix + "/pulls":
            body = kwargs["json"]
            assert body["draft"] is False
            pr = {
                "id": 1007, "number": 7, "html_url": "https://github.com/owner/repo/pull/7",
                "created_at": "2026-10-15T12:00:00Z", "state": "open", "draft": False, "merged": False,
                "head": {"ref": body["head"], "sha": self.refs[body["head"]], "repo": {"id": 123}},
                "base": {"ref": body["base"], "repo": {"id": 123, "full_name": "owner/repo"}},
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
    class SourceGit:
        validate_tree_paths = GitHub.validate_tree_paths
        def target(self, value): return 123 if value in {123, "owner/repo"} else 999
        def connection_version(self): return "current-canonical-version"
        async def request(self, *args, **kwargs): return await provider.request(*args, **kwargs)
        async def installation_token(self, **kwargs): return "synthetic-token"
        def saved_credentials(self): return {}
    async def source_call(function): return await function(SimpleNamespace(github=SourceGit()))
    service = SimpleNamespace(
        database=product.database, product=SimpleNamespace(identity=product.adapter),
        connectors=SimpleNamespace(github=SourceGit()), check=check, source_call=source_call,
        store=SimpleNamespace(rows=lambda *_args: []),
    )
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


async def test_uncertain_creation_uses_readback_and_never_reposts(publication):
    f = publication
    f.provider.lost_create = True
    request = create_request()
    await reserve(f, "create_pull_request", request)
    with pytest.raises(ConnectorError):
        await f.publisher.create(f.ctx, request)
    result = await f.publisher.create(f.ctx, request)
    assert result["number"] == 7
    assert len([item for item in f.provider.calls if item[0] == "POST" and item[1].endswith("/pulls")]) == 1


async def test_unconfirmed_invisible_creation_retains_write_barrier(publication):
    f = publication
    f.provider.lost_create, f.provider.visible = True, False
    request = create_request()
    identity = await reserve(f, "create_pull_request", request)
    with pytest.raises(ConnectorError):
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
    with pytest.raises(ConnectorError, match="regular files"):
        await f.publisher.create(f.ctx, request)
    assert not [item for item in f.provider.calls if item[0] != "GET"]
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
