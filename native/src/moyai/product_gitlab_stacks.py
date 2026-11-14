"""Moyai-managed ordered GitLab MR chains, with atomic membership projections.

GitLab has no GitHub-style native stack identity. These are explicitly named
managed chains, not fabricated provider stack objects or merge approvals.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING

from pydantic_ai.exceptions import ToolFailed
from sqlalchemy import select, text

from .gitlab_workflow_types import AddToStack, CreateStack, Page, StackMember, StackRead
from .product_github_models import ProductGitOperation as Operation
from .product_gitlab_publication import Publications, digest
from .product_gitlab_workflows import Workflows

if TYPE_CHECKING:
    from pydantic_ai import RunContext

    from .contracts import RunScope


class Stacks:
    def __init__(self, publisher: Publications):
        self.p, self.w = publisher, Workflows(publisher)

    @staticmethod
    def chain(members: list[StackMember]) -> None:
        if not 2 <= len(members) <= 100 or len({member.number for member in members}) != len(members):
            raise ToolFailed("A stack requires 2–100 distinct MRs in bottom-to-top order.")
        if any(lower.head_branch != upper.base_branch for lower, upper in pairwise(members)):
            raise ToolFailed("Every upper MR must target the previous MR's exact source branch.")

    async def source(self, stack_id: str) -> Operation:
        p = self.p
        async with p.database.sessions() as db:
            row = await db.get(Operation, stack_id)
        if row is None or not p.ours(row) or row.kind != "gl_create_stack" or not p.document(row).get("stack"):
            raise ToolFailed("That managed MR stack is not in the selected GitLab project.")
        return row

    async def snapshot(self, row: Operation) -> dict:
        stack = self.p.document(row)["stack"]
        members = []
        for member in stack["members"]:
            mr = await self.w.read_mr(member["number"])
            members.append({
                "number": mr.iid, "head_sha": mr.sha, "head_branch": mr.source_branch,
                "base_branch": mr.target_branch,
            })
        actual = {
            "stack_id": row.id, "kind": "moyai_managed_mr_chain", **self.p.identity_scope,
            "members": members, "generation": stack["generation"],
        }
        self.chain([StackMember.model_validate(member) for member in members])
        return {**actual, "revision": digest(actual)}

    async def read(self, request: StackRead | Page) -> dict:
        p = self.p
        await p.check_target(request)
        if isinstance(request, StackRead):
            return await self.snapshot(await self.source(request.stack_id))
        rows = [row for row in await p.rows(["create_stack"]) if p.document(row).get("stack")]
        rows.sort(key=lambda row: (row.created_at, row.id))
        start = (request.page - 1) * request.per_page
        selected = rows[start:start + request.per_page]
        return {"kind": "moyai_managed_mr_chain", "items": [
            {"stack_id": row.id, **p.document(row)["stack"]} for row in selected
        ], "next_page": request.page + 1 if start + len(selected) < len(rows) else None}

    async def write(self, ctx: RunContext[RunScope], kind: str, request: CreateStack | AddToStack) -> dict:
        p = self.p
        async with p.attempt(ctx, kind, request) as row:
            document = p.document(row)
            if document.get("result"):
                return document["result"]
            # Atomic projection includes a durable terminal result. Recovery
            # never reconstructs it from subsequently changed provider heads.
            if document.get("stack_result"):
                return await p.finish(row, document["stack_result"])
            source = await self.source(request.stack_id) if isinstance(request, AddToStack) else row
            if isinstance(request, AddToStack):
                snapshot = await self.snapshot(source)
                if snapshot["revision"] != request.revision or snapshot["members"] != [
                    member.model_dump() for member in request.members
                ]:
                    raise ToolFailed("The stack membership, head or base changed. Read its complete snapshot again.")
                members = [*request.members, *request.merge_requests]
                generation = snapshot["generation"]
            else:
                members, generation = request.merge_requests, 0
            self.chain(members)
            for member in members:
                mr = await self.w.admitted_mr(
                    row, member.number, member.head_sha, member.base_branch, branch_write=True,
                )
                if mr.source_branch != member.head_branch:
                    raise ToolFailed("A stack member's source branch changed.")
            frozen = [member.model_dump(mode="json") for member in members]
            await p.freeze(row, stack_id=source.id, generation=generation, members=frozen)
            # Each member is independently checked again just before projection.
            for member in members:
                await self.w.admitted_mr(
                    row, member.number, member.head_sha, member.base_branch, branch_write=True,
                )
            async with p.database.sessions.begin() as db:
                await p.authorize(db)
                # Shared project lock serializes membership across users/tasks,
                # including first creates where no stack row exists to lock.
                lock = int(digest({"stack_membership": p.identity_scope})[:15], 16)
                await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
                current = await db.get(Operation, row.id, with_for_update=True)
                if current is None:
                    raise ToolFailed("The stack operation disappeared.")
                p.require_attempt(current, row)
                doc = p.document(current)
                await p.check_authorities(db, row, doc)
                if doc.get("stack_result"):
                    # Another invocation projected this request while this one
                    # waited. The original operation can settle outside the lock.
                    raise ToolFailed("The stack projection was acknowledged concurrently; inspect publication_status.")
                stack_row = current if source.id == row.id else await db.get(Operation, source.id, with_for_update=True)
                if stack_row is None or not p.ours(stack_row):
                    raise ToolFailed("The managed stack disappeared.")
                stack_doc = p.document(stack_row)
                old = stack_doc.get("stack", {})
                if old.get("generation", 0) != generation:
                    raise ToolFailed("The managed stack membership changed concurrently.")
                all_stacks = await db.scalars(select(Operation).where(
                    Operation.repository_id == p.target, Operation.kind == "gl_create_stack",
                ))
                requested = {member.number for member in request.merge_requests}
                for candidate in all_stacks:
                    if candidate.id == source.id or not p.ours(candidate):
                        continue
                    occupied = {member["number"] for member in p.document(candidate).get("stack", {}).get("members", [])}
                    if occupied & requested:
                        raise ToolFailed("A new member already belongs to another managed MR stack.")
                result = {
                    "stack_id": source.id, **p.identity_scope, "kind": "moyai_managed_mr_chain",
                    "members": frozen, "generation": generation + 1,
                }
                result["revision"] = digest(result)
                stack_doc["stack"] = {"members": frozen, "generation": generation + 1}
                if stack_row.id == current.id:
                    doc = stack_doc
                else:
                    stack_row.document = p.database.security.seal(stack_doc)
                doc["stack_result"] = result
                current.document = p.database.security.seal(doc)
                row.document = current.document
            return await p.finish(row, result)
