"""Native GitHub stacks with complete membership and per-PR authorization."""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue, TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed

from .contracts import RunScope
from .github_workflow_types import (
    AddToStack,
    CreateStack,
    ExistingStack,
    Stack,
    StackList,
    StackMember,
    StackRead,
)
from .product_github_workflows import JSON_OBJECT, Workflows, parse, stack_revision

if TYPE_CHECKING:
    from .product_github_publication import Publications

API_VERSION = "2026-03-10"
MEMBERS = TypeAdapter(list[StackMember])


class Stacks:
    def __init__(self, publisher: Publications):
        self.publisher = publisher
        self.workflows = Workflows(publisher)

    async def read(self, request: StackList | StackRead) -> dict[str, JsonValue]:
        p = self.publisher
        await p.check_target(request)
        token = (await p.service.credentials(p.scope, p.grant))["access_token"]
        repo = await self.workflows.repository(token)
        prefix = "/repos/" + repo.full_name + "/stacks"
        if isinstance(request, StackRead):
            value = parse(Stack, await p.request(
                "GET", prefix + f"/{request.stack_number}", token, api_version=API_VERSION,
            ))
            self.validate(value, request.stack_number)
            return {"data": value.model_dump(mode="json"), "members": MEMBERS.dump_python(value.pull_requests, mode="json"),
                    "revision": stack_revision(value), "repository_id": p.target}
        data = await p.request("GET", prefix, token, api_version=API_VERSION, params={
            "page": request.page, "per_page": request.per_page,
            **({"pull_request": request.pull_request} if request.pull_request else {}),
        })
        if not isinstance(data, list) or len(data) > request.per_page:
            raise ToolFailed("GitHub returned an invalid stack page.")
        # List summaries have a smaller PR shape; only a complete stack read is a write precondition.
        return {"items": [JSON_OBJECT.validate_python(item) for item in data],
                "next_page": request.page + 1 if len(data) == request.per_page else None}

    def validate(self, stack: Stack, number: int | None = None) -> None:
        if number is not None and stack.number != number:
            raise ToolFailed("GitHub returned a different stack.")
        self.members(stack.pull_requests)

    def members(self, members: list[StackMember]) -> None:
        if not members or len(members) > 100 or len({member.number for member in members}) != len(members):
            raise ToolFailed("Select 1–100 distinct PRs in their bottom-to-top order.")
        if any(member.base.repo.id != self.publisher.target for member in members):
            raise ToolFailed("Every stack member must belong to the selected repository.")

    async def write(
        self, ctx: RunContext[RunScope], kind: Literal["create_stack", "add_to_stack"],
        request: CreateStack | AddToStack | ExistingStack,
    ) -> dict[str, JsonValue]:
        p = self.publisher
        row = await p.operation(ctx, kind, request)
        saved = p.document(row).get("result")
        if saved:
            return await p.finish(row, saved)
        members = request.pull_requests if isinstance(request, CreateStack) else [
            *request.members, *(request.pull_requests if isinstance(request, AddToStack) else []),
        ]
        additions = request.pull_requests if isinstance(request, (CreateStack, AddToStack)) else []
        self.members(members)
        acknowledgment = p.document(row).get("steps", {}).get("stack")
        if acknowledgment:
            expected_id = p.document(row)["intent"]["stack_id"]
            return await p.finish(row, self.confirm(kind, request, members, acknowledgment, expected_id))
        token = await p.token()
        repo = await self.workflows.repository(token)
        prefix = "/repos/" + repo.full_name + "/stacks"
        for member in members:
            if member.merged_at is not None:
                current = parse(StackMember, await p.request(
                    "GET", self.workflows.prefix + f"/pulls/{member.number}", token,
                ))
            else:
                pr = await self.workflows.pr(
                    token, member.number, member.head.sha, branch_write=True, operation=row,
                )
                current = StackMember.model_validate(pr.model_dump())
            if current != member:
                raise ToolFailed("A stack member's head, base, repository or state changed.")
        for member in additions:
            if member.merged_at is not None or member.state != "open":
                raise ToolFailed("Only open, unmerged PRs may join a stack.")
            existing = await p.request("GET", prefix, token, api_version=API_VERSION, params={
                "pull_request": member.number, "page": 1, "per_page": 100,
            })
            if existing != []:
                raise ToolFailed("A new member already belongs to a stack or its membership is unconfirmed.")
        before = None
        if isinstance(request, ExistingStack):
            before = parse(Stack, await p.request(
                "GET", prefix + f"/{request.stack_number}", token, api_version=API_VERSION,
            ))
            self.validate(before, request.stack_number)
            if stack_revision(before) != request.revision or before.pull_requests != request.members:
                raise ToolFailed("Stack membership changed. Read its full snapshot again.")
        if additions:
            chain = members if isinstance(request, CreateStack) else [request.members[-1], *additions]
            if any(lower.head.repo.id != upper.base.repo.id or lower.head.ref != upper.base.ref
                   for lower, upper in pairwise(chain)):
                raise ToolFailed("Stack members must form a bottom-to-top base/head chain.")
        await self.workflows.freeze(
            row, members=MEMBERS.dump_python(members, mode="json"), stack_id=before.id if before else None,
        )
        path = prefix if isinstance(request, CreateStack) else (
            prefix + f"/{request.stack_number}/add"
        )
        payload: dict[str, JsonValue] = {"pull_requests": [member.number for member in additions]} if additions else {}
        response = await p.step(
            row, "stack", "POST", path, token, payload, api_version=API_VERSION, include_status=True,
        )
        return await p.finish(row, self.confirm(kind, request, members, response, before.id if before else None))

    def confirm(
        self, kind: str, request: CreateStack | ExistingStack, members: list[StackMember],
        response: dict[str, JsonValue], expected_id: int | None,
    ) -> dict[str, JsonValue]:
        status, data = response.get("status_code"), response.get("data")
        if status != (201 if kind == "create_stack" else 200):
            raise ToolFailed("GitHub did not confirm the stack operation.")
        after = parse(Stack, data)
        self.validate(after, request.stack_number if isinstance(request, ExistingStack) else None)
        if expected_id is not None and after.id != expected_id:
            raise ToolFailed("The stack identity changed.")
        if after.pull_requests != members or after.base.ref != members[0].base.ref:
            raise ToolFailed("GitHub did not confirm the requested member ordering.")
        return {
            "outcome": "created" if kind == "create_stack" else "added",
            "data": after.model_dump(mode="json"), "stack_number": after.number,
            "remaining_prs": [member.number for member in after.pull_requests],
        }
