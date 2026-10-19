"""Real permission-token logic and SQL receipts, with GitHub protocol fixtures."""

from __future__ import annotations

import asyncio
import copy
import os
import threading
from dataclasses import replace

import pytest
import test_product_github_publication as support
from app.connector_errors import ConnectorError
from app.github import Change, GitHub
from pydantic_ai.exceptions import ToolFailed

from moyai.contracts import NewMessage
from moyai.effects import PendingEffectError
from moyai.github_branch_policy import protection_failure, protection_response
from moyai.github_request_policy import PROTECTION
from moyai.github_workflow_types import OpenPullRequest, PublicationStatus, PublishBranch
from moyai.models import Effect
from moyai.product_github_workflows import Workflows
from moyai.workspace_recovery import require_settled

# The actual response from the production installation's read-only query.
DENIED = {
    "data": {"repository": None},
    "errors": [{
        "type": "FORBIDDEN", "path": ["repository", "branchProtectionRules"],
        "extensions": {"saml_failure": False, "ip_allow_list_failure": False},
        "locations": [{"line": 2, "column": 50}],
        "message": "Resource not accessible by integration",
    }],
}

admitted_product = support.admitted_product
product_database = support.product_database
publication = support.publication
postgres = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")


class TokenProvider:
    """Replace only network/config boundaries, retaining the real token method."""

    installation_token = GitHub.installation_token

    def __init__(self, administration: str | None = None):
        self.permissions = {"contents": "write", "metadata": "read", "pull_requests": "write"}
        if administration:
            self.permissions["administration"] = administration
        self.tokens, self.issued, self.calls = {}, {}, []
        self.token_lock = asyncio.Lock()

    def target(self, value, credentials):
        assert value == 123
        return value

    def connected_ids(self, credentials):
        return [123]

    def app_config(self):
        return {"id": 456}

    def app_jwt(self, config):
        return "synthetic-app-jwt"

    async def installation(self, credentials):
        return {"permissions": dict(self.permissions)}

    async def request(self, method, path, **kwargs):
        assert (method, path) == ("POST", "/app/installations/789/access_tokens")
        assert kwargs["json"]["repository_ids"] == [123]
        scopes = kwargs["json"]["permissions"]
        assert all(self.permissions.get(name) in {level, "write"} for name, level in scopes.items())
        token = f"synthetic-token-{len(self.issued)}"
        self.issued[token] = copy.deepcopy(scopes)
        self.calls.append((method, path, copy.deepcopy(kwargs["json"])))
        return {"token": token, "expires_at": "2099-01-01T00:00:00Z"}


async def test_missing_installation_permission_fails_before_issuing_protection_token():
    provider = TokenProvider()
    with pytest.raises(ConnectorError, match="Administration: read") as caught:
        await provider.installation_token({"installation_id": 789}, repository=123, protection=True)
    assert caught.value.dispatched is False
    assert caught.value.code == "github_branch_protection_permission"
    assert provider.calls == []


@pytest.mark.parametrize("installed", ["read", "write"])
async def test_inspection_and_write_tokens_have_separate_minimal_scopes_and_caches(installed):
    provider = TokenProvider(installed)
    credentials = {"installation_id": 789}
    write = await provider.installation_token(credentials, repository=123, write=True)
    inspection = await provider.installation_token(credentials, repository=123, protection=True)
    assert provider.issued[write] == {"contents": "write", "pull_requests": "write"}
    assert provider.issued[inspection] == {"administration": "read"}
    assert write != inspection
    assert await provider.installation_token(credentials, repository=123, write=True) == write
    assert await provider.installation_token(credentials, repository=123, protection=True) == inspection
    assert len(provider.calls) == 2


@pytest.mark.parametrize("other", [{"write": True}, {"rules": True}, {"ci": "checks"}, {"coding": "anything"}, {"workflows": True}])
async def test_inspection_scope_cannot_be_combined_with_other_capabilities(other):
    with pytest.raises(ValueError):
        await TokenProvider("read").installation_token(
            {"installation_id": 789}, repository=123, protection=True, **other,
        )


@pytest.mark.parametrize("partial", [False, True])
def test_real_forbidden_response_remains_specific_even_with_partial_data(partial):
    value = copy.deepcopy(DENIED)
    if partial:
        value["data"]["repository"] = {
            "databaseId": 123,
            "branchProtectionRules": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}},
        }
    with pytest.raises(ToolFailed, match="branch-protection inspection.*FORBIDDEN"):
        protection_response(value)


@pytest.mark.parametrize("value", [{}, {"data": {"repository": None}}, {"errors": [{"type": "RATE_LIMITED"}]}])
def test_unverifiable_protection_never_becomes_permission_to_publish(value):
    with pytest.raises(ToolFailed, match="[Pp]rotection"):
        protection_response(value)


@postgres
@pytest.mark.parametrize("legacy", [False, True])
async def test_permission_repair_retries_from_new_call_and_opens_one_pr(publication, legacy):
    f = publication
    provider = TokenProvider()
    source = f.service.connectors.github
    source.saved_credentials = lambda: {"installation_id": 789}
    source.installation_token = provider.installation_token
    original_request = f.provider.request
    graph_tokens = []

    async def request(method, path, **kwargs):
        if path == "/graphql" and kwargs["json"]["query"] == PROTECTION:
            graph_tokens.append(kwargs["token"])
            if provider.issued[kwargs["token"]].get("administration") != "read":
                return copy.deepcopy(DENIED)
        return await original_request(method, path, **kwargs)

    f.provider.request = request
    change = PublishBranch(repository_id=123, base_sha="a" * 40, branch="feature/permission-repro",
                           title="Permission reproduction", request_key="permission-repro",
                           files=[Change(path="hello.txt", content="Updated")])
    original_effect = await support.reserve(f, "publish_branch", change)
    original_row = await f.publisher.operation(f.ctx, "publish_branch", change)
    tools = support.toolset(f.service, f.scope, f.grant)
    definitions = await tools.get_tools(f.ctx)
    with pytest.raises(ToolFailed, match="Administration: read") as refusal:
        await tools.call_tool("publish_branch", definitions["publish_branch"].args_validator.validate_python(
            {"request": change.model_dump(mode="json")}), f.ctx, definitions["publish_branch"])
    status = await f.publisher.status(PublicationStatus(
        repository_id=123, request_key=change.request_key, operation="publish_branch",
    ))
    assert status["attempted_steps"] == status["confirmed_steps"] == []
    assert f.provider.refs == {} and f.provider.commits == 0
    assert graph_tokens == []
    assert status["status"] == "not_dispatched"
    assert "No repository write was attempted" in str(refusal.value)
    assert "same request_key" in str(refusal.value)
    async with f.product.database.sessions() as db:
        await require_settled(db, f.scope.session_id)
        assert (await db.get(Effect, original_effect)).state == "not_dispatched"

    if legacy:
        # A pre-fix failure is settled as failed when its turn ends. No write
        # markers exist; a new execution must take ownership before dispatch.
        async with f.product.database.sessions.begin() as db:
            old_effect = await db.get(Effect, original_effect)
            old_effect.state, old_effect.settled_at = "pending", None
            row = await db.get(type(original_row), original_row.id)
            document = f.publisher.document(row)
            document.pop("preflight_error")
            row.document = f.product.database.security.seal(document)
        await f.product.database.fail(f.scope.turn_id, "GitHub permission missing")
        turn = await f.product.database.submit(f.product.actor, f.scope.session_id, NewMessage(
            prompt="Permission has been approved; retry the publication", request_key="retry-after-permission-repair",
            connections=[f.grant.id],
        ))
        assert (await f.product.database.claim_next()).id == turn.id
        f.scope = (await f.product.database.prepare(turn.id)).scope
        f.ctx = replace(f.ctx, deps=f.scope, run_id=turn.id)
        f.publisher = support.Publications(f.service, f.scope, f.grant)

    f.ctx.tool_call_id = "fresh-call-after-rejection"
    provider.permissions["administration"] = "read"
    new_effect = await support.reserve(f, "publish_branch", change)
    result = await Workflows(f.publisher).publish_branch(f.ctx, change)
    assert result["branch"] == change.branch
    assert f.provider.commits == 1
    assert len([c for c in f.provider.calls if c[0] == "POST" and c[1].endswith("/git/refs")]) == 1
    assert graph_tokens and all(provider.issued[token] == {"administration": "read"} for token in graph_tokens)
    for method, path, kwargs in f.provider.calls:
        if method == "POST" and path != "/graphql":
            assert provider.issued[kwargs["token"]] == {"contents": "write", "pull_requests": "write"}
    assert await Workflows(f.publisher).publish_branch(f.ctx, change) == result
    assert f.provider.commits == 1

    # A delayed original worker cannot dispatch after refusal or after takeover.
    with pytest.raises(ToolFailed, match="refused or replaced"):
        await f.publisher.step(original_row, "late-blob", "POST", "/repositories/123/git/blobs", "synthetic-token", {})
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, original_effect)).state == ("failed" if legacy else "not_dispatched")
        assert (await db.get(Effect, new_effect)).state == "confirmed"
        await require_settled(db, f.scope.session_id)

    pr = OpenPullRequest(repository_id=123, base_branch="main", head_branch=change.branch,
                         expected_head_sha=result["commit"], title="Permission repair", body="Test",
                         request_key="open-repaired-branch")
    f.ctx.tool_call_id = "open-repaired-pr"
    await support.reserve(f, "open_pull_request", pr)
    opened = await Workflows(f.publisher).open_existing(f.ctx, pr)
    assert opened["draft"] is False
    assert await Workflows(f.publisher).open_existing(f.ctx, pr) == opened
    assert len(f.provider.pulls) == 1


@pytest.mark.parametrize("flag, message", [("saml_failure", "SAML"), ("ip_allow_list_failure", "egress IP")])
def test_organization_restrictions_name_the_required_action(flag, message):
    value = copy.deepcopy(DENIED)
    value["errors"][0]["extensions"][flag] = True
    with pytest.raises(ToolFailed, match=message):
        protection_response(value)


@pytest.mark.parametrize("status, message", [(401, "credential"), (403, "Administration"), (404, "repository access"),
                                              (429, "rate limit"), (503, "unavailable"), (None, "connectivity")])
def test_http_check_failures_do_not_expose_provider_values(status, message):
    error = protection_failure(ConnectorError("private-provider-response", status_code=status))
    assert message in str(error)
    assert "private-provider-response" not in str(error)


@postgres
@pytest.mark.parametrize("response", [DENIED, {"data": {"repository": None}}, {"errors": [{"type": "RATE_LIMITED"}]}])
async def test_provider_preflight_errors_settle_without_writes(publication, response):
    f = publication
    original = f.provider.request

    async def request(method, path, **kwargs):
        if path == "/graphql" and kwargs["json"]["query"] == PROTECTION:
            return copy.deepcopy(response)
        return await original(method, path, **kwargs)

    f.provider.request = request
    change = support.create_request()
    effect = await support.reserve(f, "create_pull_request", change)
    with pytest.raises(ToolFailed, match="No repository write was attempted"):
        await f.publisher.create(f.ctx, change)
    assert f.provider.commits == 0 and not f.provider.refs and not f.provider.pulls
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, effect)).state == "not_dispatched"
        await require_settled(db, f.scope.session_id)


@postgres
async def test_inflight_write_wins_over_preflight_settlement(publication):
    f = publication
    change = support.create_request()
    effect = await support.reserve(f, "create_pull_request", change)
    row = await f.publisher.operation(f.ctx, "create_pull_request", change)
    entered, release = threading.Event(), threading.Event()
    original = f.provider.request

    async def request(method, path, **kwargs):
        if method == "POST" and path.endswith("/git/blobs"):
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
            raise ConnectorError("Lost acknowledgement after dispatch")
        return await original(method, path, **kwargs)

    f.provider.request = request
    running = asyncio.create_task(f.publisher.create(f.ctx, change))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert await f.publisher.refuse_preflight(row, "Concurrent read failed") is False
        async with f.product.database.sessions() as db:
            assert (await db.get(Effect, effect)).state == "pending"
        f.ctx.tool_call_id = "blocked-replacement"
        with pytest.raises(PendingEffectError):
            await support.reserve(f, "create_pull_request", change)
    finally:
        release.set()
    with pytest.raises(ToolFailed, match="already have attempted writes"):
        await running
    status = await f.publisher.status(PublicationStatus(
        repository_id=123, request_key=change.request_key, operation="create_pull_request",
    ))
    assert status["status"] == "incomplete"
    assert status["attempted_steps"] == ["blob-0"] and status["confirmed_steps"] == []


@postgres
async def test_repeated_preflight_refusals_settle_each_fresh_reservation(publication):
    f = publication
    source = f.service.connectors.github
    source.saved_credentials = lambda: {"installation_id": 789}
    source.installation_token = TokenProvider().installation_token
    change = support.create_request()
    identities = []
    for attempt in range(3):
        f.ctx.tool_call_id = f"missing-permission-{attempt}"
        identities.append(await support.reserve(f, "create_pull_request", change))
        with pytest.raises(ToolFailed, match="No repository write was attempted"):
            await f.publisher.create(f.ctx, change)
    async with f.product.database.sessions() as db:
        assert all([(await db.get(Effect, identity)).state == "not_dispatched" for identity in identities])
        await require_settled(db, f.scope.session_id)
    assert f.provider.commits == 0 and not f.provider.pulls
