"""Supplementary real-adapter protocol/ACL checks; not actual-app/model E2E proof."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from app.gitlab import GitLab
from fastapi import HTTPException
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from sqlalchemy import func, select
from test_pull_requests import client_for

from moyai.analytics_service import unique_publications
from moyai.contracts import Grant, NewMessage, NewSession, RunScope
from moyai.effects import EffectReceipts, effect_id
from moyai.links import LinkInput, save_link
from moyai.models import Effect, Turn
from moyai.pr_publications import (
    PRPublication,
    creation_receipt,
    persist_publication,
    project_gitlab_creation,
    publication_identity,
    verified_status,
)
from moyai.product_connections import ProductConnections
from moyai.product_github_models import ProductGitOperation
from moyai.product_gitlab_publication import Publications
from moyai.product_gitlab_workflows import Workflows
from moyai.product_identity import ProductGrant
from moyai.pull_requests import PullRequestService, parse_pr_url
from moyai.slack_messages import pr_cards, without_pr_references

ORIGIN = "https://gitlab.example"
PROJECT = "team/subgroup/project"
URL = f"{ORIGIN}/{PROJECT}/-/merge_requests/12"
HEAD, BASE = "a" * 40, "b" * 40
STAMP = "2026-11-13T01:00:00+00:00"


def grant(origin=ORIGIN, project_id=7):
    return Grant(
        "abcdef12-0000-0000-0000-000000000001", "gitlab", 3, "", read_only=False,
        policy={"require_approval": False, "product": {
            "provider": "gitlab", "base_url": origin, "repository_id": project_id,
        }},
    )


def mr(**changes):
    return {
        "id": 101, "iid": 12, "project_id": 7, "source_project_id": 7, "target_project_id": 7,
        "web_url": URL, "title": "GitLab review", "description": "A real adapter response.",
        "state": "opened", "draft": False, "sha": HEAD,
        "diff_refs": {"base_sha": BASE, "head_sha": HEAD, "start_sha": BASE},
        "source_branch": "feature", "target_branch": "main", "changes_count": "1",
        "author": {"username": "author"}, "created_at": STAMP, "updated_at": STAMP,
        "merged_at": None, **changes,
    }


class CanonicalAccounts:
    # Use the actual read credential wrapper; only source-account resolution and
    # issuance are synthetic in this supplementary protocol/ACL fixture.
    read_credentials = ProductConnections.read_credentials
    _credential_lock = ProductConnections._credential_lock

    def __init__(self, actor, provider, selected):
        import asyncio

        self.actor, self.grant, self.available = actor, selected, True
        self.credentials_read = 0
        self.connectors = SimpleNamespace(gitlab=provider, locks={"gitlab": asyncio.Lock()})
        self.execution = SimpleNamespace(call=self.call)

    async def resolve_grants(self, actor_id, selections, **kwargs):
        if not self.available:
            raise PermissionError("Revoked")
        return [copy.deepcopy(self.grant)] if actor_id == self.actor.id and self.grant.id in selections else []

    async def _credentials(self, selected, *, write=False):
        assert selected == self.grant and not write and self.available
        self.credentials_read += 1
        return {"access_token": "test-private-token", "base_url": ORIGIN, "repository_ids": [7]}

    async def call(self, operation, *, authorize=None, recheck=False):
        if authorize:
            await authorize()
        result = await operation(self.connectors)
        if recheck and authorize:
            await authorize()
        return result


@pytest.fixture
async def connected(runtime, actor, monkeypatch):
    calls = []
    data = {
        "project": {"id": 7, "path_with_namespace": PROJECT, "web_url": f"{ORIGIN}/{PROJECT}", "visibility": "private"},
        "mr": mr(),
        "diffs": [{"old_path": "a.txt", "new_path": "a.txt", "diff": "@@ -1 +1 @@\n-old\n+new\n"}],
        "discussions": [{"id": "discussion", "notes": [{
            "id": 55, "body": "Please check", "author": {"username": "reviewer"}, "created_at": STAMP,
            "position": {"new_path": "a.txt", "new_line": 1}, "resolved": True,
        }]}],
        "commits": [{"id": HEAD, "message": "Change", "author_name": "Author", "committed_date": STAMP}],
        "pipelines": [
            {"id": 91, "sha": HEAD, "status": "failed", "ref": "feature", "web_url": f"{ORIGIN}/{PROJECT}/-/pipelines/91"},
            {"id": 90, "sha": "c" * 40, "status": "success"},
        ],
    }
    callback = {"during": None}

    def handler(request):
        calls.append(request)
        assert request.url.host == urlsplit(data["project"]["web_url"]).hostname
        assert request.headers["private-token"] == "test-private-token"
        path = request.url.path
        if path == "/api/v4/projects/7":
            return httpx.Response(200, json=data["project"])
        if path == "/api/v4/projects/7/merge_requests/12":
            if callback["during"]:
                callback["during"]()
            return httpx.Response(200, json=data["mr"])
        key = path.rsplit("/", 1)[-1]
        assert key in data and request.url.params["per_page"] == "100"
        return httpx.Response(200, json=data[key])

    monkeypatch.setattr("app.gitlab.GitLabTransport", lambda **kwargs: httpx.MockTransport(handler))
    provider = GitLab(None, None, SimpleNamespace(gitlab_allow_loopback_http=False), None)
    accounts = CanonicalAccounts(actor, provider, grant())
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt=URL, request_key="review"))
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Turn, turn.id)
        row.connections = '["' + accounts.grant.id + '"]'
    runtime.product_connections = accounts
    service = PullRequestService(runtime)
    return SimpleNamespace(
        service=service, session=session, turn=turn, accounts=accounts, data=data,
        calls=calls, callback=callback, runtime=runtime, actor=actor,
    )


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"), URL.replace("gitlab.example", "user@ gitlab.example"),
    URL.replace("gitlab.example", "user:secret@gitlab.example"), URL.replace("gitlab.example", "@gitlab.example"),
    URL.replace("subgroup", ".."), URL.replace("subgroup", "%2e%2e"), URL + "/diffs",
    URL.replace("/12", "/0"), URL.replace("/12", "/123456789012"), URL + "\nsecret",
])
def test_rejects_ambiguous_gitlab_links(url):
    with pytest.raises(HTTPException):
        parse_pr_url(url)


def test_nested_project_url_normalization_keeps_case_and_existing_github_shape():
    pr = parse_pr_url(URL + "?tab=diffs#note_55")
    assert (pr.provider, pr.origin, pr.repository, pr.number, pr.url) == ("gitlab", ORIGIN, PROJECT, 12, URL)
    assert LinkInput(kind="pull_request", url=URL).title == PROJECT + " !12"
    assert parse_pr_url("https://github.com/Owner/Repo/pull/12").url == "https://github.com/owner/repo/pull/12"


async def test_selected_private_mr_sections_and_typed_http_response(connected):
    f = connected
    await save_link(f.runtime.database, f.actor, f.session.id, LinkInput(kind="pull_request", url=URL))
    async with client_for(f.runtime, f.actor, f.service) as client:
        overview = await client.get(f"/api/sessions/{f.session.id}/pull-request", params={"url": URL})
        assert overview.status_code == 200
        body = overview.json()
        assert body["accessible"] and body["state"] == "open" and body["number"] == 12
        assert body["additions"] is None  # Missing statistics are never fabricated as zero.
        for tab in ("files", "discussion", "commits", "checks"):
            result = await client.get(
                f"/api/sessions/{f.session.id}/pull-request", params={"url": URL, "tab": tab},
            )
            assert result.status_code == 200, result.text
            items = result.json()["section"]["items"]
            assert len(items) == 1
            if tab == "files":
                assert items[0]["patch_status"] == "available" and items[0]["additions"] == 1
            elif tab == "discussion":
                assert items[0]["path"] == "a.txt" and items[0]["state"] == "RESOLVED"
            elif tab == "checks":
                assert items[0]["status"] == "failure" and items[0]["id"] == "pipeline-91"
        statuses = await client.post("/api/pull-requests/statuses", json={"session_ids": [f.session.id]})
        assert statuses.json()[f.session.id][0]["origin"] == "attached"
    assert all(call.url.host == "gitlab.example" for call in f.calls)


async def test_no_selected_or_matching_origin_grant_never_fetches_or_reads_credentials(connected):
    f = connected
    for value in (
        f.accounts.grant.id.replace("abcdef12", "ffffffff"),
        f.accounts.grant.id,
    ):
        if value == f.accounts.grant.id:
            f.accounts.grant.policy["product"]["base_url"] = "https://different.example"
        async with f.runtime.database.sessions.begin() as db:
            row = await db.get(Turn, f.turn.id)
            row.connections = '["' + value + '"]'
        detail = await f.service.detail(f.actor, f.session.id, URL, "overview", False)
        assert not detail["accessible"]
    assert f.calls == [] and f.accounts.credentials_read == 0


async def test_revocation_during_private_read_discards_content_and_never_falls_back(connected):
    f = connected
    f.callback["during"] = lambda: setattr(f.accounts, "available", False)
    with pytest.raises(HTTPException) as refused:
        await f.service.detail(f.actor, f.session.id, URL, "overview", False)
    assert refused.value.status_code == 409
    assert len(f.calls) == 2
    assert all(call.headers.get("private-token") for call in f.calls)


async def test_source_identity_revocation_discards_private_content(connected):
    f = connected
    available = True

    async def require_current(selected):
        assert selected == f.actor.product_grant
        if not available:
            raise HTTPException(401, "Product authority changed.")

    f.actor.product_grant = ProductGrant(f.actor.id, "a" * 64)
    f.accounts.product = SimpleNamespace(identity=SimpleNamespace(require_current=require_current))

    def revoke():
        nonlocal available
        available = False

    f.callback["during"] = revoke
    with pytest.raises(HTTPException) as refused:
        await f.service.detail(f.actor, f.session.id, URL, "overview", False)
    assert refused.value.status_code == 401
    assert len(f.calls) == 2


async def test_product_viewer_without_source_identity_cannot_read_credentials(connected):
    f = connected
    f.accounts.product = SimpleNamespace(identity=SimpleNamespace())
    result = await f.service.detail(f.actor, f.session.id, URL, "overview", False)
    assert not result["accessible"]
    assert not f.calls and f.accounts.credentials_read == 0


async def test_read_tool_policy_uses_actual_gitlab_operations(connected):
    f = connected
    f.accounts.grant.policy["allowed_tools"] = ["merge_request"]
    detail = await f.service.detail(f.actor, f.session.id, URL, "files", False)
    assert detail["accessible"]
    assert detail["section"]["items"] == [] and detail["section"]["error"]
    assert all(not request.url.path.endswith("/diffs") for request in f.calls)
    f.accounts.grant.policy["allowed_tools"].append("merge_request_diffs")
    detail = await f.service.detail(f.actor, f.session.id, URL, "files", False)
    assert len(detail["section"]["items"]) == 1


@pytest.mark.parametrize("origin", ["http://127.0.0.1:58990", "http://[::1]:58990"])
async def test_loopback_link_needs_selected_grant_and_explicit_provider_opt_in(connected, origin):
    f = connected
    local_url = URL.replace(ORIGIN, origin)
    f.accounts.grant.policy["product"]["base_url"] = origin
    f.data["project"]["web_url"] = f"{origin}/{PROJECT}"
    f.data["mr"]["web_url"] = local_url
    assert parse_pr_url(local_url).origin == origin
    result = await f.service.detail(f.actor, f.session.id, local_url, "overview", False)
    assert not result["accessible"] and not f.calls and not f.accounts.credentials_read
    f.accounts.connectors.gitlab.settings.gitlab_allow_loopback_http = True
    result = await f.service.detail(f.actor, f.session.id, local_url, "overview", False)
    assert result["accessible"]
    assert pr_cards(local_url, "")[0]["blocks"][-1]["elements"][0]["url"] == local_url


async def test_analytics_follows_verified_project_rename_but_attachment_does_not(connected):
    f = connected
    renamed = "new/subgroup/project"
    f.data["project"].update(path_with_namespace=renamed, web_url=f"{ORIGIN}/{renamed}")
    f.data["mr"].update(web_url=URL.replace(PROJECT, renamed), state="merged", merged_at=STAMP)
    target = {
        "provider": "gitlab", "origin": ORIGIN, "project_id": 7, "merge_request_id": 101,
        "repository": PROJECT, "url": URL,
    }
    scope = (await f.service.contexts(f.actor, [f.session.id]))[f.session.id]
    result = await f.service.record(scope, parse_pr_url(URL), target)
    assert not result.error and result.value["web_url"] == URL.replace(PROJECT, renamed)
    receipt = PRPublication(number=12)
    assert verified_status(receipt, result.value, target)["state"] == "merged"
    for bad in ({**target, "origin": "https://other.example"}, {**target, "project_id": 8}):
        f.calls.clear()
        assert (await f.service.record(scope, parse_pr_url(URL), bad)).error
        assert not f.calls
    detail = await f.service.detail(f.actor, f.session.id, URL, "overview", False)
    assert not detail["accessible"]


@pytest.mark.parametrize("changes", [
    {"project_id": 8}, {"iid": 13}, {"web_url": URL.replace(ORIGIN, "https://wrong.example")},
    {"draft": "false"}, {"id": True},
])
async def test_wrong_mr_identity_is_not_presented(connected, changes):
    f = connected
    f.data["mr"].update(changes)
    result = await f.service.detail(f.actor, f.session.id, URL, "overview", False)
    assert not result["accessible"]


async def test_diff_ref_change_discards_old_section(connected):
    f = connected
    detail = await f.service.detail(f.actor, f.session.id, URL, "overview", False)
    f.data["mr"]["sha"] = "c" * 40
    scope = (await f.service.contexts(f.actor, [f.session.id]))[f.session.id]
    section = await f.service.section(scope, parse_pr_url(URL), detail, "files", False)
    assert section["items"] == [] and section["error"] and section["limited"]


def test_slack_mr_cards_use_linked_identity_without_claiming_creation():
    cards = pr_cards(f"[Review changes]({URL})\nAgain: {URL}", "")
    assert len(cards) == 1
    assert cards[0]["blocks"][1]["text"]["text"] == "MR !12 · Review changes"
    button = cards[0]["blocks"][-1]["elements"][0]
    assert button["text"]["text"] == "View MR" and button["url"] == URL
    assert without_pr_references(f"MR: <{URL}>\nUseful explanation", cards) == "Useful explanation"
    assert pr_cards(f"`{URL}`", "") == []


def creation(value=None, selected=None):
    selected = selected or grant()
    scope = RunScope("s", "t", "u", "model", [selected])
    receipt = creation_receipt(
        scope, "gitlab_abcdef12_create_merge_request", {"repository_id": 7},
        value if value is not None else mr(), STAMP,
    )
    return receipt


@pytest.mark.parametrize("changes", [
    {"iid": 13}, {"project_id": 8}, {"source_project_id": 8}, {"id": True},
    {"draft": True}, {"state": "merged"}, {"created_at": "invalid"},
    {"web_url": URL.replace(ORIGIN, "https://other.example")},
])
def test_creation_rejects_unverified_receipts(changes):
    assert creation(mr(**changes)) is None


def test_creation_receipt_and_stable_identity_separate_providers_hosts_and_renames():
    receipt = creation()
    assert receipt and receipt.project_id == 7 and receipt.number == 12
    assert receipt.github_id is None and receipt.repository_id is None
    renamed = creation(mr(web_url=URL.replace(PROJECT, "new/team/name")))
    assert renamed and publication_identity(renamed) == publication_identity(receipt)
    other_origin = "https://second.example"
    other = creation(mr(web_url=URL.replace(ORIGIN, other_origin)), grant(other_origin))
    assert other and publication_identity(other) != publication_identity(receipt)
    rows = [
        PRPublication(id="github", actor_id="g", created_at=STAMP, url_key="github-url", number=12, github_id=101, repository_id=7),
        PRPublication(id="gitlab", actor_id="l", created_at=STAMP, url_key=publication_identity(receipt), number=12),
        PRPublication(id="renamed", actor_id="later", created_at=STAMP, url_key=publication_identity(renamed), number=12),
        PRPublication(id="other", actor_id="o", created_at=STAMP, url_key=publication_identity(other), number=12),
    ]
    assert [row.id for row in unique_publications(rows)] == ["github", "gitlab", "other"]


async def test_persist_requires_exact_confirmed_gitlab_operation_without_new_schema(runtime, actor, active):
    session, turn, _ = active
    selected = grant()
    scope = RunScope(session.id, turn.id, actor.id, "test", [selected])
    name = "gitlab_abcdef12_create_merge_request"
    receipt = creation_receipt(scope, name, {"repository_id": 7}, mr(), STAMP)
    assert receipt
    async with runtime.database.sessions.begin() as db:
        db.add(Effect(
            id="gitlab-effect", session_id=session.id, turn_id=turn.id, actor_id=actor.id,
            tool_name=name, state="confirmed", arguments=runtime.security.seal({}), fingerprint="f",
        ))
    with pytest.raises(ValueError, match="matching provider operation"):
        await persist_publication(runtime.database, scope, "gitlab-effect", receipt)
    document = {"provider_scope": {"provider": "gitlab", "origin": ORIGIN, "project_id": 7}, "result": mr()}
    async with runtime.database.sessions.begin() as db:
        db.add(ProductGitOperation(
            id="gitlab-operation", session_id=session.id, turn_id=turn.id, actor_id=actor.id,
            connection_id=selected.id, connection_revision=selected.revision, repository_id=7,
            kind="gl_create_mr", request_key="create-mr", fingerprint="f", original_effect_id="gitlab-effect",
            document=runtime.security.seal(document),
        ))
    await persist_publication(runtime.database, scope, "gitlab-effect", receipt)
    async with runtime.database.sessions() as db:
        row = await db.scalar(select(PRPublication).where(PRPublication.id == "gitlab-effect"))
        target = json.loads(runtime.security.unseal(row.target))
    assert row.github_id is None and row.repository_id is None
    assert target["provider"] == "gitlab" and target["project_id"] == 7 and target["merge_request_id"] == 101
    status = verified_status(row, mr(state="merged", merged_at=STAMP), target)
    assert status and status["state"] == "merged"
    assert verified_status(row, mr(id=999), target) is None
    assert verified_status(row, mr(project_id=999), target) is None


@pytest.mark.parametrize("operation,kind", [
    ("create_merge_request", "gl_create_mr"), ("open_merge_request", "gl_open_mr"),
])
async def test_real_finish_and_effect_hook_project_exact_creation_once(runtime, active, operation, kind, monkeypatch):
    """Exercise the actual typed workflow receipt -> finish -> effects projection."""
    session, turn, prepared = active
    selected = grant()
    scope = replace(prepared.scope, grants=[selected])
    name = f"gitlab_{selected.id[:8]}_{operation}"
    args = {"request": {"repository_id": 7, "request_key": "create-proof"}}
    ctx = SimpleNamespace(deps=scope, run_id=turn.id)
    call = ToolCallPart(name, args, tool_call_id="create-proof")
    identity = effect_id(ctx, call)
    row = ProductGitOperation(
        id="gitlab-boundary", session_id=session.id, turn_id=turn.id, actor_id=scope.actor_id,
        connection_id=selected.id, connection_revision=selected.revision, repository_id=7,
        kind=kind, request_key="create-proof", fingerprint="f", original_effect_id=identity,
        document=runtime.security.seal({
            "provider_scope": {"provider": "gitlab", "origin": ORIGIN, "project_id": 7},
            "intent": {"branch": "feature", "base": "main"}, "commit": HEAD,
            "attempts": ["merge-request"], "steps": {"merge-request": {"acknowledged": True, "response": mr()}},
        }),
    )
    async with runtime.database.sessions.begin() as db:
        db.add(row)
        db.add(Effect(
            id=identity, session_id=session.id, turn_id=turn.id, actor_id=scope.actor_id,
            tool_name=name, state="pending", arguments=runtime.security.seal(args), fingerprint="f",
        ))
    publisher = Publications(SimpleNamespace(database=runtime.database), scope, selected)
    result = await publisher.finish(row, Workflows(publisher).created_receipt(row, mr()))
    assert result["draft"] is False and result["iid"] == 12 and result["project_id"] == 7
    async with runtime.database.sessions() as db:
        assert (await db.get(PRPublication, identity)).turn_id == turn.id
    # This is the publisher finish integration hook: the original turn receives
    # credit even when a later turn recovers a confirmed, undelivered operation.
    assert await project_gitlab_creation(runtime.database, replace(scope, turn_id="later-turn"), row.id)
    async with runtime.database.sessions() as db:
        assert (await db.get(PRPublication, identity)).turn_id == turn.id
    capability = EffectReceipts(runtime.database)
    tool = ToolDefinition(name=name)
    for _ in range(2):
        assert await capability.after_tool_execute(ctx, call=call, tool_def=tool, args=args, result=result) == result
    async with runtime.database.sessions() as db:
        assert (await db.get(Effect, identity)).state == "confirmed"
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == 1
        publication = await db.get(PRPublication, identity)
        assert publication.github_id is None and publication.repository_id is None
        assert json.loads(runtime.security.unseal(publication.target))["project_id"] == 7

    async def fail_projection(*args):
        raise OSError("Synthetic projection failure.")

    monkeypatch.setattr(capability, "record_pr_publication", fail_projection)
    assert await capability.after_tool_execute(ctx, call=call, tool_def=tool, args=args, result=result) == result
    monkeypatch.setattr("moyai.pr_publications.persist_publication", fail_projection)
    assert await publisher.finish(row, result) == result
    async with runtime.database.sessions() as db:
        assert (await db.get(Effect, identity)).state == "confirmed"
