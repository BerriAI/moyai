"""GitHub workflows composed over native durable operations and task authority."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from sqlalchemy import select

from .contracts import RunScope
from .github_request_policy import ADVANCE, DRAFT, REBASE
from .github_workflow_types import (
    SHA,
    AuthorityRef,
    Branch,
    Commit,
    CreateBranch,
    CreateReview,
    DraftResponse,
    EditReview,
    GitReference,
    OpenPullRequest,
    PendingReview,
    ProviderObject,
    PRWrite,
    Publish,
    PublishBranch,
    PullRequest,
    Rebase,
    RebaseResponse,
    RepositoryIdentity,
    Retarget,
    Review,
    SubmitReview,
)
from .product_github_models import ProductGitOperation as Operation

if TYPE_CHECKING:
    from .product_github_publication import Publications

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
logger = logging.getLogger(__name__)


class PublicationSteps(ProviderObject):
    commit: Commit | None = None
    branch: GitReference | None = None
    pull_request: dict[str, JsonValue] | None = Field(default=None, alias="pull-request")
    branch_update: RebaseResponse | None = Field(default=None, alias="branch-update")


class PublicationSnapshot(ProviderObject):
    steps: PublicationSteps
    intent: dict[str, JsonValue] = Field(default_factory=dict)
    result: dict[str, JsonValue] | None = None
    branch: Branch | None = None
    base_branch: Branch | None = None


class RefReceiptScope(ProviderObject):
    branch: Branch
    commit: SHA


class PRReceiptScope(RefReceiptScope):
    base: Branch


def parse[T: BaseModel](schema: type[T], value: object) -> T:
    try:
        return schema.model_validate(value)
    except ValidationError as error:
        # Preserve the failing contract without logging provider response
        # bodies, input values, credentials, or validation context.
        logger.warning(
            "GitHub response validation failed: schema=%s fields=%s",
            schema.__name__,
            [{"location": item["loc"], "type": item["type"]}
             for item in error.errors(include_input=False, include_context=False, include_url=False)],
        )
        raise ToolFailed("GitHub did not return the expected repository, ref or operation identity.") from None
    except (TypeError, ValueError):
        raise ToolFailed("GitHub did not return the expected repository, ref or operation identity.") from None


class Workflows:
    def __init__(self, publisher: Publications):
        self.publisher = publisher

    @property
    def prefix(self) -> str:
        return f"/repositories/{self.publisher.target}"

    async def resume_publication(self, row: Operation) -> dict[str, JsonValue] | None:
        """Settle a saved terminal acknowledgment without consulting mutable provider state."""
        p = self.publisher
        async with p.database.sessions() as db:
            current = await db.get(Operation, row.id)
            if current is None:
                raise ToolFailed("The publication reservation disappeared.")
            row.document = current.document
            saved = parse(PublicationSnapshot, p.document(current))
        if saved.result:
            return await p.finish(row, saved.result)
        if row.kind in {"create_pull_request", "open_pull_request"}:
            if saved.steps.pull_request is None:
                return None
            commit = (
                saved.steps.commit.sha if row.kind == "create_pull_request" and saved.steps.commit
                else saved.intent.get("commit") if row.kind == "open_pull_request" else None
            )
            scope = parse(PRReceiptScope, {"base": saved.base_branch, "branch": saved.branch, "commit": commit})
            result = p.verified_pr(saved.steps.pull_request, scope.branch, scope.base, scope.commit)
            return await p.finish(row, result)
        if row.kind not in {"create_branch", "publish_branch"}:
            return None
        if saved.steps.branch is None and saved.steps.branch_update is None:
            return None
        commit = (
            saved.intent.get("commit") if row.kind == "create_branch"
            else saved.steps.commit.sha if saved.steps.commit else None
        )
        scope = parse(RefReceiptScope, {"branch": saved.intent.get("branch"), "commit": commit})
        if saved.steps.branch_update is not None:
            response = saved.steps.branch_update
            if (
                row.kind != "publish_branch" or response.errors
                or response.data is None or response.data.updateRefs is None
            ):
                raise ToolFailed("The saved branch update was not confirmed; it will not be repeated.")
            parse(Commit, {"sha": saved.intent.get("expected_head_sha")})
        elif saved.steps.branch is None or (
            saved.steps.branch.ref != "refs/heads/" + scope.branch or saved.steps.branch.object.sha != scope.commit
        ):
            raise ToolFailed("The saved branch acknowledgment differs from the frozen publication.")
        return await p.finish(row, {"repository_id": p.target, "branch": scope.branch, "commit": scope.commit})

    async def repository(self, token: str) -> RepositoryIdentity:
        repo = parse(RepositoryIdentity, await self.publisher.request("GET", self.prefix, token))
        if repo.id != self.publisher.target:
            raise ToolFailed("The repository identity changed.")
        return repo

    async def commit_at(self, token: str, ref: str) -> str:
        return parse(Commit, await self.publisher.request(
            "GET", self.prefix + "/commits/" + quote(ref, safe=""), token,
        )).sha

    async def ref(self, token: str, branch: str) -> GitReference | None:
        value = await self.publisher.request(
            "GET", self.prefix + "/git/ref/heads/" + quote(branch, safe=""), token, missing=True,
        )
        if value is None:
            return None
        result = parse(GitReference, value)
        if result.ref != "refs/heads/" + branch:
            raise ToolFailed("GitHub returned a different branch.")
        return result

    async def writable(self, token: str, branch: str, *, existing: bool) -> None:
        from .github_branch_policy import require_writable
        await require_writable(self.publisher, await self.repository(token), token, branch, existing=existing)

    async def branch_owned(self, branch: str, head: str | None = None) -> bool:
        p = self.publisher
        token = (await p.service.credentials(p.scope, p.grant))["access_token"]
        current = await self.ref(token, branch)
        if current is None or (head is not None and current.object.sha != head):
            return False
        anchors: set[str] = set()
        advances: list[tuple[str, str]] = []
        async with p.database.sessions() as db:
            rows = await db.scalars(select(Operation).where(
                Operation.repository_id == p.target, Operation.connection_id == p.grant.id,
                Operation.connection_revision == p.grant.revision,
                Operation.kind.in_(["create_pull_request", "create_branch", "publish_branch",
                                    "update_pull_request", "rebase_pull_request"]),
            ))
            for row in rows:
                document = p.document(row)
                if row.kind == "publish_branch" and document.get("steps", {}).get("branch-update"):
                    intent = document.get("intent", {})
                    if intent.get("branch") != branch or not intent.get("expected_head_sha"):
                        continue
                    try:
                        response = RebaseResponse.model_validate(document["steps"]["branch-update"])
                    except ValidationError:
                        continue
                    if not response.errors and response.data and response.data.updateRefs:
                        advances.append((intent["expected_head_sha"], document["steps"]["commit"]["sha"]))
                    continue
                if row.kind == "update_pull_request":
                    response = document.get("steps", {}).get("advance-branch")
                    if not response:
                        continue
                    try:
                        ref = GitReference.model_validate(response)
                    except ValidationError:
                        continue
                    if (
                        ref.ref == "refs/heads/" + branch
                        and ref.object.sha == document.get("steps", {}).get("commit", {}).get("sha")
                    ):
                        advances.append((document["base_sha"], ref.object.sha))
                    continue
                if row.kind == "rebase_pull_request":
                    intent = document.get("intent", {})
                    receipt = document.get("steps", {}).get("rebase", {})
                    if (
                        intent.get("branch") == branch and intent.get("owned_branch") is True
                        and isinstance((receipt.get("data") or {}).get("updateRefs"), dict)
                        and not receipt.get("errors")
                    ):
                        advances.append((intent["previous"], document["steps"]["commit"]["sha"]))
                    continue
                response = document.get("steps", {}).get("branch")
                if not response or document.get("branch") != branch:
                    continue
                try:
                    ref = GitReference.model_validate(response)
                except ValidationError:
                    continue
                created = document.get("created_commit") or document.get("steps", {}).get("commit", {}).get("sha")
                if ref.ref == "refs/heads/" + branch and ref.object.sha == created:
                    anchors.add(ref.object.sha)
        # Only acknowledged Moyai transitions extend provenance. Repository
        # ancestry alone cannot distinguish an externally recreated branch.
        successors: dict[str, set[str]] = {}
        for before, after in advances:
            successors.setdefault(before, set()).add(after)
        frontier = list(anchors)
        while frontier:
            for after in successors.get(frontier.pop(), set()):
                if after not in anchors:
                    anchors.add(after)
                    frontier.append(after)
        return current.object.sha in anchors

    async def freeze(self, row: Operation, **intent: JsonValue) -> dict[str, JsonValue]:
        p = self.publisher
        async with p.database.sessions.begin() as db:
            await p.authorize(db)
            current = await db.get(Operation, row.id, with_for_update=True)
            if current is None:
                raise ToolFailed("The publication reservation disappeared.")
            p.require_attempt(current, row)
            document = JSON_OBJECT.validate_python(p.document(current))
            if "intent" in document and document["intent"] != intent:
                raise ToolFailed("The publication intent changed. Inspect its receipt before choosing a new request key.")
            document["intent"] = intent
            current.document = p.database.security.seal(document)
            row.document = current.document
            return document

    async def create_ref(self, row: Operation, token: str, branch: str, commit: str) -> None:
        p = self.publisher
        await p.save(row, branch=branch, created_commit=commit)
        await self.writable(token, branch, existing=False)
        current = await self.ref(token, branch)
        acknowledged = p.document(row).get("steps", {}).get("branch")
        if current:
            if not acknowledged or parse(GitReference, acknowledged) != current or current.object.sha != commit:
                raise ToolFailed("This branch already exists or its creation was not acknowledged. It was not overwritten.")
            return
        if acknowledged:
            raise ToolFailed("The previously created branch was deleted. It will not be recreated.")
        response = await p.step(row, "branch", "POST", self.prefix + "/git/refs", token, {
            "ref": "refs/heads/" + branch, "sha": commit,
        })
        ref = parse(GitReference, response)
        if ref.ref != "refs/heads/" + branch or ref.object.sha != commit:
            raise ToolFailed("GitHub did not acknowledge creation of the exact branch.")

    async def create_branch(self, ctx: RunContext[RunScope], request: CreateBranch) -> dict[str, JsonValue]:
        p = self.publisher
        async with p.attempt(ctx, "create_branch", request) as row:
            recovered = await self.resume_publication(row)
            if recovered is not None:
                return recovered
            token = await p.token()
            commit = await self.commit_at(token, request.commit_sha)
            if commit != request.commit_sha:
                raise ToolFailed("The source commit changed.")
            await self.freeze(row, branch=request.branch, commit=commit)
            await self.create_ref(row, token, request.branch, commit)
            return await p.finish(row, {"repository_id": p.target, "branch": request.branch, "commit": commit})

    async def publish_branch(self, ctx: RunContext[RunScope], request: PublishBranch) -> dict[str, JsonValue]:
        from app.github import workflow_changes
        p = self.publisher
        async with p.attempt(ctx, "publish_branch", request) as row:
            recovered = await self.resume_publication(row)
            if recovered is not None:
                return recovered
            token = await p.token(workflows=workflow_changes(request))
            if request.expected_head_sha is not None:
                return await self.advance_branch(row, token, request)
            await self.freeze(row, branch=request.branch, source=request.base_sha)
            await self.writable(token, request.branch, existing=False)
            commit = await p.commit(row, request, token)
            await self.create_ref(row, token, request.branch, commit)
            return await p.finish(row, {"repository_id": p.target, "branch": request.branch, "commit": commit})

    async def advance_branch(
        self, row: Operation, token: str, request: PublishBranch,
    ) -> dict[str, JsonValue]:
        p = self.publisher
        if not await self.branch_owned(request.branch, request.expected_head_sha):
            raise ToolFailed("An existing branch requires exact acknowledged Moyai ownership.")
        await self.writable(token, request.branch, existing=True)
        await self.freeze(
            row, branch=request.branch, source=request.base_sha, expected_head_sha=request.expected_head_sha,
        )
        commit = await p.commit(row, request, token)
        await self.writable(token, request.branch, existing=True)
        repo = await self.repository(token)
        result = await p.step(row, "branch-update", "POST", "/graphql", token, {
            "query": ADVANCE,
            "variables": {"repositoryId": repo.node_id, "name": "refs/heads/" + request.branch,
                          "beforeOid": request.expected_head_sha, "afterOid": commit},
        })
        response = parse(RebaseResponse, result)
        if response.errors or response.data is None or response.data.updateRefs is None:
            raise ToolFailed("GitHub did not confirm the atomic expected-head branch update.")
        return await p.finish(row, {"repository_id": p.target, "branch": request.branch, "commit": commit})

    async def create(self, ctx: RunContext[RunScope], request: Publish) -> dict[str, JsonValue]:
        from app.github import workflow_changes
        p = self.publisher
        async with p.attempt(ctx, "create_pull_request", request) as row:
            recovered = await self.resume_publication(row)
            if recovered is not None:
                return recovered
            token = await p.token(workflows=workflow_changes(request))
            repo = await self.repository(token)
            base = request.base_branch or repo.default_branch
            branch = request.head_branch or f"moyai/{p.scope.session_id[:12]}/{row.id[:16]}"
            if base == branch:
                raise ToolFailed("The PR base and head must differ.")
            current = await self.commit_at(token, base)
            if current != request.base_sha:
                comparison = await p.request("GET", self.prefix + f"/compare/{request.base_sha}...{current}", token)
                if comparison.get("status") not in {"ahead", "identical"}:
                    raise ToolFailed("The requested source is not an ancestor of the selected base branch.")
            await self.freeze(row, base=base, branch=branch, source=request.base_sha)
            await p.save(row, base_branch=base, branch=branch, repository=repo.full_name)
            await self.writable(token, branch, existing=False)
            commit = await p.commit(row, request, token)
            await self.create_ref(row, token, branch, commit)
            return await self.open(row, token, repo, base, branch, commit, request.title, request.body)

    async def open_existing(self, ctx: RunContext[RunScope], request: OpenPullRequest) -> dict[str, JsonValue]:
        p = self.publisher
        async with p.attempt(ctx, "open_pull_request", request) as row:
            recovered = await self.resume_publication(row)
            if recovered is not None:
                return recovered
            token = await p.token()
            repo = await self.repository(token)
            await self.commit_at(token, request.base_branch)
            head = await self.ref(token, request.head_branch)
            if head is None or head.object.sha != request.expected_head_sha:
                raise ToolFailed("The selected head changed. Read its current SHA before opening a PR.")
            await self.freeze(row, base=request.base_branch, branch=request.head_branch, commit=request.expected_head_sha)
            await p.save(row, base_branch=request.base_branch, branch=request.head_branch, repository=repo.full_name)
            return await self.open(
                row, token, repo, request.base_branch, request.head_branch, request.expected_head_sha,
                request.title, request.body,
            )

    async def open(
        self, row: Operation, token: str, repo: RepositoryIdentity, base: str, branch: str,
        commit: str, title: str, body: str,
    ) -> dict[str, JsonValue]:
        p = self.publisher
        recovered = await self.resume_publication(row)
        if recovered is not None:
            return recovered
        head = await self.ref(token, branch)
        if head is None or head.object.sha != commit:
            raise ToolFailed("The selected head changed before PR creation.")
        pulls = await p.request("GET", self.prefix + "/pulls", token, params={
            "state": "all", "head": repo.full_name.split("/")[0] + ":" + branch, "base": base,
        })
        if not isinstance(pulls, list) or len(pulls) > 1:
            raise ToolFailed("GitHub did not return a unique PR readback.")
        if pulls:
            # Readback cannot establish authorship of a preexisting or ambiguous creation.
            raise ToolFailed("A matching PR exists without this operation's creation acknowledgment. Inspect GitHub.")
        value = await p.step(row, "pull-request", "POST", self.prefix + "/pulls", token, {
            "title": title, "body": body, "head": branch, "base": base,
            "draft": False, "maintainer_can_modify": True,
        })
        return await p.finish(row, p.verified_pr(value, branch, base, commit))

    async def pr(
        self, token: str, number: int, head: str, *, branch_write: bool = False,
        closed: bool = False, lifecycle: bool = False, operation: Operation | None = None,
    ) -> PullRequest:
        p = self.publisher
        owned = await p.authority(number, base_sha=head, branch_write=branch_write, lifecycle=lifecycle)
        pr = parse(PullRequest, await p.request("GET", self.prefix + f"/pulls/{number}", token))
        if (
            pr.number != number or pr.base.repo.id != p.target or pr.head.repo.id != p.target
            or pr.head.sha != head or pr.merged or pr.merged_at or (pr.state != "open" and not closed)
            or pr.head.ref != owned["head"]["ref"]
            or (owned.get("base", {}).get("ref") and pr.base.ref != owned["base"]["ref"])
        ):
            raise ToolFailed("The PR is merged, closed, or no longer matches its authorized head/base.")
        if branch_write:
            await self.writable(token, pr.head.ref, existing=True)
        if operation is not None:
            await p.bind_authority(
                operation, parse(AuthorityRef, owned), number=number, head=head, base=pr.base.ref, lifecycle=lifecycle,
            )
        return pr

    async def rebase(self, ctx: RunContext[RunScope], request: Rebase) -> dict[str, JsonValue]:
        from app.github import workflow_changes
        p = self.publisher
        row = await p.operation(ctx, "rebase_pull_request", request)
        saved = p.document(row).get("result")
        if saved:
            return await p.finish(row, saved)
        acknowledged = p.document(row).get("steps", {}).get("rebase")
        if acknowledged:
            document = p.document(row)
            commit = document["steps"]["commit"]["sha"]
            branch = document["intent"]["branch"]
            response = parse(RebaseResponse, acknowledged)
            if response.errors or response.data is None or response.data.updateRefs is None:
                raise ToolFailed("The earlier rebase was rejected; it will not be repeated.")
            return await p.finish(row, {
                "repository_id": p.target, "number": request.number, "branch": branch, "commit": commit,
            })
        token = await p.token(workflows=workflow_changes(request))
        pr = await self.pr(token, request.number, request.expected_head_sha, branch_write=True, operation=row)
        if pr.base.ref != request.base_branch or await self.commit_at(token, request.base_branch) != request.base_sha:
            raise ToolFailed("Rebase requires the PR's current base branch and exact current base commit.")
        await self.freeze(
            row, number=request.number, base=request.base_branch, source=request.base_sha,
            branch=pr.head.ref, previous=request.expected_head_sha,
            owned_branch=await self.branch_owned(pr.head.ref, request.expected_head_sha),
        )
        commit = await p.commit(row, request, token)
        current = await self.pr(token, request.number, request.expected_head_sha, branch_write=True, operation=row)
        repo = await self.repository(token)
        result = await p.step(row, "rebase", "POST", "/graphql", token, {
            "query": REBASE,
            "variables": {"repositoryId": repo.node_id, "name": "refs/heads/" + current.head.ref,
                          "beforeOid": request.expected_head_sha, "afterOid": commit},
        })
        response = parse(RebaseResponse, result)
        if response.errors or response.data is None or response.data.updateRefs is None:
            raise ToolFailed("GitHub did not confirm the atomic expected-head rebase.")
        return await p.finish(row, {
            "repository_id": p.target, "number": request.number, "branch": current.head.ref, "commit": commit,
        })

    async def lifecycle(
        self, ctx: RunContext[RunScope], kind: Literal[
            "close_pull_request", "reopen_pull_request", "retarget_pull_request", "convert_pull_request_to_draft",
        ], request: PRWrite | Retarget,
    ) -> dict[str, JsonValue]:
        p = self.publisher
        row = await p.operation(ctx, kind, request)
        saved = p.document(row).get("result")
        if saved:
            return await p.finish(row, saved)
        if isinstance(request, Retarget):
            previous = p.document(row)
            acknowledgment = previous.get("steps", {}).get("pull-request")
            if acknowledgment:
                self.lifecycle_receipt(kind, request, acknowledgment, previous["lifecycle_scope"])
                await self.retarget_authority(
                    request.number, previous["intent"]["base"], request.base_branch, operation=row,
                )
                return await p.finish(row, acknowledgment)
        acknowledged = p.document(row).get("steps", {}).get(
            "draft" if kind == "convert_pull_request_to_draft" else "pull-request",
        )
        if acknowledged:
            self.lifecycle_receipt(kind, request, acknowledged, p.document(row)["lifecycle_scope"])
            return await p.finish(row, acknowledged)
        token = await p.token()
        pr = await self.pr(
            token, request.number, request.head_sha, closed=kind == "reopen_pull_request",
            lifecycle=True, operation=row,
        )
        if isinstance(request, Retarget):
            if request.base_branch == pr.head.ref:
                raise ToolFailed("A PR cannot target its own head branch.")
            await self.commit_at(token, request.base_branch)
            body: dict[str, JsonValue] = {"base": request.base_branch}
        else:
            body = {"state": "open" if kind == "reopen_pull_request" else "closed"}
        scope = {"number": pr.number, "node_id": pr.node_id, "branch": pr.head.ref}
        await self.freeze(row, number=pr.number, head=pr.head.sha, base=pr.base.ref, lifecycle_scope=scope)
        await p.save(row, lifecycle_scope=scope)
        if kind == "convert_pull_request_to_draft":
            value = await p.step(row, "draft", "POST", "/graphql", token, {
                "query": DRAFT, "variables": {"id": pr.node_id},
            })
        else:
            value = await p.step(row, "pull-request", "PATCH", self.prefix + f"/pulls/{pr.number}", token, body)
            changed_pr = parse(PullRequest, value)
            if changed_pr.number != pr.number or (
                changed_pr.head.repo.id, changed_pr.head.ref
            ) != (pr.head.repo.id, pr.head.ref):
                raise ToolFailed("The PR identity changed during the action.")
            if isinstance(request, Retarget):
                if changed_pr.base.ref != request.base_branch or changed_pr.base.repo.id != p.target:
                    raise ToolFailed("GitHub did not confirm the selected PR base.")
                await self.retarget_authority(pr.number, pr.base.ref, request.base_branch, operation=row)
            elif changed_pr.state != body["state"]:
                raise ToolFailed("GitHub did not confirm the requested PR state.")
        self.lifecycle_receipt(kind, request, value, scope)
        return await p.finish(row, JSON_OBJECT.validate_python(value))

    def lifecycle_receipt(
        self, kind: str, request: PRWrite | Retarget, value: object, scope: dict[str, JsonValue],
    ) -> None:
        if kind == "convert_pull_request_to_draft":
            response = parse(DraftResponse, value)
            changed = response.data.convertPullRequestToDraft if response.data else None
            pr = changed.pullRequest if changed else None
            if response.errors or pr is None or pr.number != request.number or not pr.isDraft:
                raise ToolFailed("GitHub did not confirm draft conversion.")
            return
        pr = parse(PullRequest, value)
        if (
            pr.number != request.number or pr.base.repo.id != self.publisher.target or pr.merged or pr.merged_at
            or pr.head.repo.id != self.publisher.target or pr.head.ref != scope["branch"]
            or pr.node_id != scope["node_id"]
        ):
            raise ToolFailed("The lifecycle receipt belongs to a different or merged PR.")
        if isinstance(request, Retarget):
            if pr.base.ref != request.base_branch:
                raise ToolFailed("GitHub did not confirm the selected PR base.")
        elif pr.state != ("open" if kind == "reopen_pull_request" else "closed"):
            raise ToolFailed("GitHub did not confirm the requested PR state.")

    async def retarget_authority(
        self, number: int, before: str, after: str, *, operation: Operation | None = None,
    ) -> None:
        p = self.publisher
        async with p.database.sessions.begin() as db:
            current = await db.get(Operation, operation.id, with_for_update=True) if operation else None
            projection = p.document(current) if current else None
            if projection is not None and projection.get("authority_projection_applied"):
                return
            candidates = (await db.execute(select(Operation.id, Operation.document).where(
                Operation.repository_id == p.target, Operation.connection_id == p.grant.id,
                Operation.connection_revision == p.grant.revision,
                Operation.kind.in_(["create_pull_request", "open_pull_request", "pr_write_access", "pr_lifecycle_access"]),
            ))).all()
            identities = [
                identity for identity, encrypted in candidates
                if json.loads(p.database.security.unseal(encrypted)).get("result", {}).get("number") == number
            ]
            for identity in sorted(identities):
                row = await db.get(Operation, identity, with_for_update=True, populate_existing=True)
                if row is None:
                    continue
                document = p.document(row)
                receipt = document.get("result", {})
                effective_base = document.get("authorized_base") or receipt.get("base", {}).get("ref")
                if receipt.get("number") != number or effective_base != before:
                    continue
                if row.kind in {"pr_write_access", "pr_lifecycle_access"}:
                    document["revoked"] = True
                else:
                    # Keep the creation receipt immutable; this is the live authority projection.
                    document["authorized_base"] = after
                row.document = p.database.security.seal(document)
            if current is not None and projection is not None:
                projection["authority_projection_applied"] = True
                current.document = p.database.security.seal(projection)
                if operation is not None:
                    operation.document = current.document

    async def review(
        self, ctx: RunContext[RunScope], kind: Literal[
            "create_review", "submit_review", "edit_pending_review", "delete_pending_review",
        ], request: CreateReview | PendingReview | SubmitReview | EditReview,
    ) -> dict[str, JsonValue]:
        p = self.publisher
        row = await p.operation(ctx, kind, request)
        saved = p.document(row).get("result")
        if saved:
            return await p.finish(row, saved)
        acknowledged = p.document(row).get("steps", {}).get("review")
        if acknowledged:
            self.review_receipt(request, acknowledged)
            return await p.finish(row, acknowledged)
        token = await p.token()
        await self.pr(token, request.number, request.head_sha, operation=row)
        path = self.prefix + f"/pulls/{request.number}/reviews"
        if isinstance(request, CreateReview):
            await self.freeze(row, number=request.number, head=request.head_sha)
            await p.save(row, review_scope={"number": request.number})
            body: dict[str, JsonValue] = {"commit_id": request.head_sha, "body": request.body}
            if request.comments:
                body["comments"] = [comment.model_dump(mode="json") for comment in request.comments]
            if request.event:
                body["event"] = request.event
            method = "POST"
        else:
            await self.pending_review(request)
            pending = parse(Review, await p.request("GET", path + f"/{request.review_id}", token))
            if pending.state != "PENDING" or pending.commit_id != request.head_sha:
                raise ToolFailed("Only this task's unchanged pending review can be changed.")
            path += f"/{request.review_id}"
            if isinstance(request, SubmitReview):
                method, path = "POST", path + "/events"
                body = {"body": request.body, "event": request.event}
            elif isinstance(request, EditReview):
                method = "PUT"
                body = {"body": request.body}
            else:
                method = "DELETE"
                body = {}
        value = await p.step(row, "review", method, path, token, body)
        self.review_receipt(request, value)
        return await p.finish(row, JSON_OBJECT.validate_python(value))

    @staticmethod
    def review_receipt(
        request: CreateReview | PendingReview | SubmitReview | EditReview, value: object,
    ) -> None:
        result = parse(Review, value)
        event = request.event if isinstance(request, (CreateReview, SubmitReview)) else None
        expected = "COMMENTED" if event == "COMMENT" else "CHANGES_REQUESTED" if event == "REQUEST_CHANGES" else "PENDING"
        if (
            result.commit_id != request.head_sha or result.state != expected
            or (isinstance(request, PendingReview) and result.id != request.review_id)
        ):
            raise ToolFailed("GitHub did not confirm the exact non-approval review action.")

    async def pending_review(self, request: PendingReview) -> None:
        p = self.publisher
        async with p.database.sessions() as db:
            rows = await db.scalars(select(Operation).where(
                Operation.session_id == p.scope.session_id, Operation.actor_id == p.scope.actor_id,
                Operation.turn_id == p.scope.turn_id, Operation.repository_id == p.target,
                Operation.connection_id == p.grant.id, Operation.connection_revision == p.grant.revision,
                Operation.kind == "create_review",
            ))
            for row in rows:
                document = p.document(row)
                result = document.get("result", {})
                intent = document.get("review_scope", {})
                if (
                    result.get("id") == request.review_id and result.get("state") == "PENDING"
                    and result.get("commit_id") == request.head_sha and intent.get("number") == request.number
                ):
                    return
        raise ToolFailed("This task has no confirmed ownership of that pending review.")


def stack_revision(value: BaseModel) -> str:
    return hashlib.sha256(json.dumps(value.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
