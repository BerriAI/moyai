"""Exact-head GitLab branch/MR workflows over durable native intent/effects."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

from app.github import Change
from pydantic import TypeAdapter
from pydantic_ai.exceptions import ToolFailed

from . import gitlab_transport as transport
from .gitlab_branch_policy import require_writable
from .gitlab_workflow_types import (
    BranchInfo,
    Changes,
    Commit,
    CreateBranch,
    CreateMR,
    MergeRequest,
    MRWrite,
    OpenMR,
    PublishBranch,
    RebaseMR,
    Retarget,
    UpdateMR,
)
from .product_gitlab_publication import KINDS, Publications, parse

if TYPE_CHECKING:
    from pydantic_ai import RunContext

    from .contracts import RunScope
    from .product_github_models import ProductGitOperation as Operation


class Workflows:
    def __init__(self, publisher: Publications):
        self.p = publisher

    async def commit_at(self, ref: str) -> str:
        return parse(Commit, await self.p.request(
            "GET", self.p.prefix + "/repository/commits/" + quote(ref, safe=""),
        )).id

    async def branch(self, branch: str) -> BranchInfo | None:
        value = await self.p.request(
            "GET", self.p.prefix + "/repository/branches/" + quote(branch, safe=""), missing=True,
        )
        if value is None:
            return None
        result = parse(BranchInfo, value)
        if result.name != branch:
            raise ToolFailed("GitLab returned a different branch.")
        return result

    async def writable(self, branch: str, *, existing: bool) -> BranchInfo | None:
        return await require_writable(self.p, await self.p.project(), branch, existing=existing)

    def validate_mr(self, value: object, number: int | None = None) -> MergeRequest:
        mr = parse(MergeRequest, value)
        if (
            mr.project_id != self.p.target or mr.source_project_id != self.p.target
            or mr.target_project_id != self.p.target or (number is not None and mr.iid != number)
            or not mr.web_url.startswith(self.p.origin + "/")
            or not mr.web_url.endswith("/-/merge_requests/" + str(mr.iid))
        ):
            raise ToolFailed("The MR is not in the exact selected GitLab source/target project.")
        return mr

    async def read_mr(self, number: int) -> MergeRequest:
        return self.validate_mr(await self.p.request("GET", self.p.prefix + f"/merge_requests/{number}"), number)

    async def get_mr(self, number: int, head: str, base: str, *, closed: bool = False) -> MergeRequest:
        mr = await self.read_mr(number)
        if (
            mr.sha != head or mr.target_branch != base or mr.merged_at or mr.state == "merged"
            or mr.state not in ({"opened", "closed"} if closed else {"opened"})
        ):
            raise ToolFailed("The GitLab MR head, base or unmerged state changed.")
        return mr

    async def admitted_mr(
        self, row: Operation, number: int, head: str, base: str, *,
        branch_write: bool = False, lifecycle: bool = False, closed: bool = False,
    ) -> MergeRequest:
        # A selected-project read can distinguish stale input before current-head
        # lineage checks; stale owned heads must not be mistaken for external MRs.
        mr = await self.get_mr(number, head, base, closed=closed)
        authority = await self.p.authority(
            number, head=head, base=base, branch_write=branch_write, lifecycle=lifecycle,
        )
        receipt = parse(MergeRequest, self.p.document(authority)["result"])
        if mr.id != receipt.id or mr.source_branch != receipt.source_branch:
            raise ToolFailed("The MR no longer has its admitted source identity.")
        if branch_write:
            current = await self.writable(mr.source_branch, existing=True)
            if current is None or current.commit.id != head:
                raise ToolFailed("The MR source branch no longer matches the exact admitted head.")
        await self.p.bind(row, authority, mr, lifecycle=lifecycle)
        return mr

    async def branch_owned(self, branch: str, head: str) -> bool:
        current = await self.branch(branch)
        if current is None or current.commit.id != head:
            return False
        anchors: set[str] = set()
        edges: dict[str, set[str]] = {}
        for row in await self.p.rows([
            "create_branch", "publish_branch", "create_merge_request", "update_merge_request", "rebase_merge_request",
        ]):
            document = self.p.document(row)
            receipt = document["steps"].get("push", {}).get("response", {})
            intent = document.get("intent", {})
            if (
                receipt.get("branch") != branch or receipt.get("acknowledged") is not True
                or receipt.get("origin") != self.p.origin
                or receipt.get("project") != intent.get("project")
                or receipt.get("commit") != document.get("commit")
            ):
                continue
            if receipt.get("before") is None:
                anchors.add(receipt["commit"])
            elif intent.get("owned_branch") is True:
                edges.setdefault(receipt["before"], set()).add(receipt["commit"])
        frontier = list(anchors)
        while frontier:
            for after in edges.get(frontier.pop(), set()):
                if after not in anchors:
                    anchors.add(after)
                    frontier.append(after)
        return head in anchors

    def push_receipt(self, row: Operation, response: object) -> dict:
        document = self.p.document(row)
        intent = document["intent"]
        if not isinstance(response, dict) or any(
            response.get(key) != value for key, value in {
                "provider": "gitlab", "origin": self.p.origin, "project": intent["project"],
                "branch": intent["branch"], "before": intent.get("previous"),
                "commit": document["commit"], "acknowledged": True,
            }.items()
        ):
            raise ToolFailed("GitLab did not acknowledge the exact frozen ref transition.")
        return {**self.p.identity_scope, "repository_id": self.p.target,
                "branch": intent["branch"], "commit": document["commit"],
                **({"number": intent["number"], "web_url": intent["url"]} if "number" in intent else {})}

    async def push(
        self, row: Operation, *, prepared: transport.PreparedCommit | None = None, rebase: bool = False,
    ) -> dict:
        p = self.p
        document, credentials = p.document(row), await p.credentials(write=True)
        intent = document["intent"]
        current = await self.writable(intent["branch"], existing=intent.get("previous") is not None)
        if (current.commit.id if current else None) != intent.get("previous"):
            raise ToolFailed("The branch changed externally; it was not overwritten.")
        if "number" in intent:
            await self.admitted_mr(row, intent["number"], intent["previous"], intent["base"], branch_write=True)
        elif intent.get("previous") and not await self.branch_owned(intent["branch"], intent["previous"]):
            raise ToolFailed("The existing branch lacks acknowledged Moyai ownership.")
        async def execute(bound):
            # Guard at the final trusted transport boundary. Server-side
            # protection and the exact lease remain authoritative during push.
            await bound.gitlab.request(
                "GET", p.prefix, token=credentials["access_token"], base_url=p.origin,
            )
            return await transport.push(
                base_url=p.origin, project_path=intent["project"], token=credentials["access_token"],
                branch=intent["branch"], commit=document["commit"], expected_head=intent.get("previous"),
                bundle=prepared.bundle if prepared else None, allow_rebase=rebase,
                allow_loopback_http=p.allow_loopback_http,
            )
        response = await p.dispatch(row, "push", lambda: p.service.execution.call(
            execute, authorize=lambda: p.service.check(p.scope, p.grant),
        ))
        return self.push_receipt(row, response)

    async def create_branch(self, ctx: RunContext[RunScope], request: CreateBranch) -> dict:
        p = self.p
        async with p.attempt(ctx, "create_branch", request) as row:
            document = p.document(row)
            if document.get("result"):
                return document["result"]
            if "push" in document["steps"]:
                return await p.finish(row, self.push_receipt(row, document["steps"]["push"]["response"]))
            project = await p.project()
            if await self.commit_at(request.commit_sha) != request.commit_sha:
                raise ToolFailed("GitLab returned a different source commit.")
            await p.freeze(row, project=project.path_with_namespace, branch=request.branch,
                           source=request.commit_sha, previous=None, owned_branch=True)
            await p.save(row, commit=request.commit_sha)
            return await p.finish(row, await self.push(row))

    async def files(self, row: Operation, ctx: RunContext[RunScope], request: Changes) -> list[Change]:
        p = self.p
        saved = p.document(row).get("files")
        if saved is not None:
            return TypeAdapter(list[Change]).validate_python(saved)
        if request.directory:
            from .product_gitlab_checkout import staged_files
            files = await staged_files(p, ctx, request)
        else:
            files = request.files or []
        if not files and not isinstance(request, RebaseMR):
            raise ToolFailed("Publication requires at least one staged or explicit file change.")
        await p.save(row, files=[change.model_dump(mode="json") for change in files])
        return files

    async def publish(
        self, ctx: RunContext[RunScope], kind: str, request: PublishBranch | CreateMR | UpdateMR | RebaseMR,
    ) -> dict:
        p = self.p
        async with p.attempt(ctx, kind, request) as row:
            document = p.document(row)
            if document.get("result"):
                return document["result"]
            if "merge-request" in document["steps"]:
                return await p.finish(row, self.created_receipt(row, document["steps"]["merge-request"]["response"]))
            if "push" in document["steps"]:
                result = self.push_receipt(row, document["steps"]["push"]["response"])
                if not isinstance(request, CreateMR):
                    return await p.finish(row, result)
                return await self.open(row, request.title, request.body)
            project = await p.project()
            files = await self.files(row, ctx, request)
            previous, base, number, url = None, None, None, None
            owned = True
            if isinstance(request, PublishBranch):
                branch, previous = request.branch, request.expected_head_sha
                if previous and not await self.branch_owned(branch, previous):
                    raise ToolFailed("Publishing an existing branch requires acknowledged exact-head ownership.")
            elif isinstance(request, CreateMR):
                branch = request.head_branch or f"moyai/{p.scope.session_id[:12]}/{row.id[:16]}"
                base = request.base_branch or project.default_branch
                if branch == base or await self.commit_at(base) != request.base_sha:
                    raise ToolFailed("MR creation requires a different feature branch and the exact current base.")
            else:
                previous = request.expected_head_sha if isinstance(request, RebaseMR) else request.base_sha
                mr = await self.admitted_mr(
                    row, request.number, previous, request.base_branch, branch_write=True,
                )
                branch, base, number, url = mr.source_branch, mr.target_branch, mr.iid, mr.web_url
                owned = await self.branch_owned(branch, previous)
                if isinstance(request, RebaseMR) and await self.commit_at(base) != request.base_sha:
                    raise ToolFailed("Prepared rebase requires the exact current target branch commit.")
            await p.freeze(
                row, project=project.path_with_namespace, branch=branch, source=request.base_sha,
                previous=previous, owned_branch=owned,
                **({"base": base} if base else {}), **({"number": number, "url": url} if number else {}),
            )
            await self.writable(branch, existing=previous is not None)
            credentials = await p.credentials(write=True)
            async def prepare(bound):
                await bound.gitlab.request("GET", p.prefix, token=credentials["access_token"], base_url=p.origin)
                return await transport.prepare_commit(
                    base_url=p.origin, project_path=project.path_with_namespace, base_sha=request.base_sha,
                    token=credentials["access_token"], files=files, title=request.title,
                    timestamp=int(datetime.fromisoformat(row.created_at).timestamp()), allow_loopback_http=p.allow_loopback_http,
                )
            prepared = await p.service.execution.call(
                prepare, authorize=lambda: p.service.check(p.scope, p.grant), recheck=True,
            )
            known = p.document(row).get("commit")
            if known is not None and known != prepared.sha:
                raise ToolFailed("The frozen prepared Git commit changed.")
            await p.save(row, commit=prepared.sha)
            result = await self.push(row, prepared=prepared, rebase=isinstance(request, RebaseMR))
            if isinstance(request, CreateMR):
                return await self.open(row, request.title, request.body)
            return await p.finish(row, result)

    def created_receipt(self, row: Operation, value: object) -> dict:
        mr = self.validate_mr(value)
        intent = self.p.document(row)["intent"]
        if (
            mr.draft or mr.state != "opened" or mr.source_branch != intent["branch"]
            or mr.target_branch != intent["base"] or mr.sha != self.p.document(row)["commit"]
        ):
            raise ToolFailed("GitLab did not confirm the exact ready-for-review MR.")
        return mr.model_dump(mode="json")

    async def open(self, row: Operation, title: str, body: str) -> dict:
        p = self.p
        document = p.document(row)
        if "merge-request" in document["steps"]:
            return await p.finish(row, self.created_receipt(row, document["steps"]["merge-request"]["response"]))
        intent = document["intent"]
        current = await self.branch(intent["branch"])
        if current is None or current.commit.id != document["commit"]:
            raise ToolFailed("The source branch changed before MR creation.")
        existing = await p.request("GET", p.prefix + "/merge_requests", params={
            "scope": "all", "state": "all", "source_branch": intent["branch"],
            "target_branch": intent["base"], "per_page": 100, "page": 1,
        })
        if existing != []:
            raise ToolFailed("A matching MR exists without this operation's acknowledgment; inspect GitLab.")
        value = await p.step(row, "merge-request", "POST", p.prefix + "/merge_requests", {
            "source_branch": intent["branch"], "target_branch": intent["base"], "title": title,
            "description": body, "remove_source_branch": False, "allow_collaboration": False,
        })
        return await p.finish(row, self.created_receipt(row, value))

    async def open_existing(self, ctx: RunContext[RunScope], request: OpenMR) -> dict:
        p = self.p
        async with p.attempt(ctx, "open_merge_request", request) as row:
            document = p.document(row)
            if document.get("result"):
                return document["result"]
            if "merge-request" in document["steps"]:
                return await p.finish(row, self.created_receipt(row, document["steps"]["merge-request"]["response"]))
            project = await p.project()
            await self.commit_at(request.base_branch)
            await p.freeze(row, project=project.path_with_namespace, base=request.base_branch,
                           branch=request.head_branch, source=request.expected_head_sha, owned_branch=False)
            await p.save(row, commit=request.expected_head_sha)
            return await self.open(row, request.title, request.body)

    async def lifecycle(self, ctx: RunContext[RunScope], kind: str, request: MRWrite | Retarget) -> dict:
        p = self.p
        async with p.attempt(ctx, kind, request) as row:
            document = p.document(row)
            if document.get("result"):
                return document["result"]
            response = document["steps"].get("lifecycle", {}).get("response")
            if response is None:
                mr = await self.admitted_mr(
                    row, request.number, request.head_sha, request.base_branch,
                    lifecycle=True, closed=kind == "reopen_merge_request",
                )
                if isinstance(request, Retarget):
                    if request.target_branch == mr.source_branch:
                        raise ToolFailed("An MR cannot target its own source branch.")
                    await self.commit_at(request.target_branch)
                    body = {"target_branch": request.target_branch}
                elif kind == "convert_merge_request_to_draft":
                    body = {"title": mr.title if mr.title.startswith("Draft: ") else "Draft: " + mr.title}
                else:
                    body = {"state_event": "reopen" if kind == "reopen_merge_request" else "close"}
                await p.freeze(row, mr=mr.model_dump(mode="json"), body=body)
                await self.get_mr(request.number, request.head_sha, request.base_branch,
                                  closed=kind == "reopen_merge_request")
                response = await p.step(row, "lifecycle", "PUT", p.prefix + f"/merge_requests/{mr.iid}", body)
            before = parse(MergeRequest, p.document(row)["intent"]["mr"])
            after = self.validate_mr(response, request.number)
            if (
                after.id != before.id or after.source_branch != before.source_branch
                or after.sha != request.head_sha or after.merged_at
                or after.target_branch != (request.target_branch if isinstance(request, Retarget) else request.base_branch)
                or (kind == "convert_merge_request_to_draft" and not after.draft)
                or (kind == "close_merge_request" and after.state != "closed")
                or (kind == "reopen_merge_request" and after.state != "opened")
            ):
                raise ToolFailed("GitLab did not acknowledge the exact requested MR lifecycle change.")
            if isinstance(request, Retarget):
                await self.retarget_authority(row, before, after)
            return await p.finish(row, after.model_dump(mode="json"))

    async def retarget_authority(self, operation: Operation, before: MergeRequest, after: MergeRequest) -> None:
        p = self.p
        rows = await p.rows(["create_merge_request", "open_merge_request",
                             "request_merge_request_write_access", "request_mr_lifecycle_access"])
        async with p.database.sessions.begin() as db:
            current = await db.get(type(operation), operation.id, with_for_update=True)
            if current is None:
                raise ToolFailed("The retarget operation disappeared.")
            projection = p.document(current)
            if projection.get("authority_projected"):
                return
            for candidate in sorted(rows, key=lambda item: item.id):
                row = await db.get(type(operation), candidate.id, with_for_update=True)
                if row is None:
                    continue
                document = p.document(row)
                value = document.get("result", {})
                if value.get("id") == before.id and (
                    document.get("authorized_base") or value.get("target_branch")
                ) == before.target_branch:
                    if row.kind in {KINDS["request_merge_request_write_access"], KINDS["request_mr_lifecycle_access"]}:
                        document["revoked"] = True
                    else:
                        document["authorized_base"] = after.target_branch
                    row.document = p.database.security.seal(document)
            projection["authority_projected"] = True
            current.document = p.database.security.seal(projection)
            operation.document = current.document
