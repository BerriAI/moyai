"""Task-owned draft note reviews; individual publication and real review decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.connector_errors import ConnectorError
from pydantic_ai.exceptions import ToolFailed

from .gitlab_request_policy import REQUEST_CHANGES, REVIEW_CAPABILITY
from .gitlab_workflow_types import CreateReview, EditReview, MergeRequest, PendingReview, SubmitReview
from .product_github_models import ProductGitOperation as Operation
from .product_gitlab_publication import Publications, parse
from .product_gitlab_workflows import Workflows

if TYPE_CHECKING:
    from pydantic_ai import RunContext

    from .contracts import RunScope


async def review_capability(p: Publications) -> dict:
    try:
        value = await p.request("POST", "/graphql", json={"query": REVIEW_CAPABILITY})
    except (ToolFailed, ConnectorError):
        return {"request_changes": False, "reason": "The GitLab review-decision schema could not be verified."}
    fields = ((value.get("data") or {}).get("__type") or {}).get("fields") if isinstance(value, dict) else None
    available = (
        not value.get("errors") and isinstance(fields, list)
        and any(item.get("name") == "mergeRequestRequestChanges" for item in fields if isinstance(item, dict))
    ) if isinstance(value, dict) else False
    return {"request_changes": available,
            "reason": "Provider schema supports the mutation; project permission/tier is checked at dispatch."
            if available else "This GitLab schema does not expose a request-changes decision."}


class Reviews:
    def __init__(self, publisher: Publications):
        self.p, self.w = publisher, Workflows(publisher)

    async def require_decision(self, event: str | None) -> None:
        if event == "REQUEST_CHANGES" and not (await review_capability(self.p))["request_changes"]:
            raise ToolFailed("REQUEST_CHANGES is unavailable on this GitLab instance; no substitute comment was published.")

    @staticmethod
    def draft_receipt(value: object, mr: MergeRequest, note: dict, expected_id: int | None = None) -> dict:
        if not isinstance(value, dict) or (
            type(value.get("id")) is not int or value["id"] <= 0
            or type(value.get("author_id")) is not int or value["author_id"] <= 0
            or value.get("merge_request_id") != mr.id or value.get("note") != note["note"]
            or (expected_id is not None and value["id"] != expected_id)
            or value.get("resolve_discussion") is True or value.get("discussion_id") is not None
            or value.get("commit_id") not in {None, mr.sha}
        ):
            raise ToolFailed("GitLab did not acknowledge this task's exact draft note.")
        position = note.get("position")
        actual = value.get("position") or {}
        if position and any(actual.get(key) != item for key, item in position.items()):
            raise ToolFailed("GitLab returned a draft note at a different diff position.")
        if not position and actual.get("head_sha"):
            raise ToolFailed("GitLab returned an unexpected inline draft note.")
        return {"id": value["id"], "author_id": value["author_id"], "note": note["note"],
                "position": position, "commit_id": mr.sha}

    async def claim(self, source: Operation, row: Operation) -> None:
        p = self.p
        async with p.database.sessions.begin() as db:
            current = await db.get(Operation, source.id, with_for_update=True)
            if current is None:
                raise ToolFailed("The pending review reservation disappeared.")
            document = p.document(current)
            if document.get("review_state", "PENDING") != "PENDING":
                raise ToolFailed("This task's review is no longer pending.")
            owner = document.get("review_consumer")
            if owner and owner != row.id:
                previous = await db.get(Operation, owner)
                if previous is None or not p.document(previous).get("result"):
                    raise ToolFailed("Another review operation is incomplete or uncertain; inspect its receipt first.")
            document["review_consumer"] = row.id
            current.document = p.database.security.seal(document)
            source.document = current.document
            if source.id == row.id:
                row.document = current.document

    async def owned(self, request: PendingReview) -> Operation:
        p = self.p
        async with p.database.sessions() as db:
            row = await db.get(Operation, request.review_id)
        if row is None or not p.ours(row) or (
            row.kind != "gl_create_review" or row.actor_id != p.scope.actor_id
            or row.session_id != p.scope.session_id or row.turn_id != p.scope.turn_id
        ):
            raise ToolFailed("Only this task's acknowledged pending review can be edited or submitted.")
        document = p.document(row)
        mr = parse(MergeRequest, document.get("intent", {}).get("mr"))
        if (
            mr.iid != request.number or mr.sha != request.head_sha or mr.target_branch != request.base_branch
            or not document.get("review_notes") or document.get("review_state", "PENDING") != "PENDING"
            or len(document["review_notes"]) != len(document["intent"].get("notes", []))
        ):
            raise ToolFailed("The pending review identity, head, base or state changed.")
        return row

    async def current_note(self, mr: MergeRequest, note: dict) -> None:
        value = await self.p.request(
            "GET", self.p.prefix + f"/merge_requests/{mr.iid}/draft_notes/{note['id']}",
        )
        checked = self.draft_receipt(value, mr, note, note["id"])
        if checked["author_id"] != note["author_id"]:
            raise ToolFailed("The pending note's author changed.")

    async def decision(self, row: Operation, mr: MergeRequest) -> None:
        p = self.p
        acknowledged = p.document(row)["steps"].get("request-changes")
        if acknowledged:
            value = acknowledged["response"]
        else:
            project = await p.project()
            value = await p.step(row, "request-changes", "POST", "/graphql", {
                "query": REQUEST_CHANGES, "variables": {"projectPath": project.path_with_namespace, "iid": str(mr.iid)},
            })
        self.decision_receipt(value, mr)

    def decision_receipt(self, value: object, mr: MergeRequest) -> None:
        p = self.p
        mutation = (value.get("data") or {}).get("mergeRequestRequestChanges") if isinstance(value, dict) else None
        changed = mutation.get("mergeRequest") if isinstance(mutation, dict) else None
        if (
            not isinstance(value, dict) or value.get("errors") or not mutation or mutation.get("errors")
            or not isinstance(changed, dict) or changed.get("id") != f"gid://gitlab/MergeRequest/{mr.id}"
            or changed.get("iid") != str(mr.iid) or changed.get("projectId") != p.target
            or changed.get("sourceProjectId") != p.target or changed.get("targetProjectId") != p.target
            or changed.get("diffHeadSha") != mr.sha
        ):
            raise ToolFailed("GitLab did not confirm a request-changes decision. Notes may have published; inspect the partial receipt.")

    async def recover(
        self, row: Operation, request: CreateReview | PendingReview | SubmitReview | EditReview,
    ) -> dict | None:
        """Project only durable terminal acknowledgments, independent of later head changes."""
        p, document = self.p, self.p.document(row)
        terminal = document.get("terminal_result")
        if not document.get("intent"):
            return None
        mr = parse(MergeRequest, document["intent"]["mr"])
        source_id = row.id if isinstance(request, CreateReview) else document["intent"]["review_id"]
        async with p.database.sessions() as db:
            source = await db.get(Operation, source_id)
        if source is None or not p.ours(source):
            raise ToolFailed("The original task-owned review receipt disappeared.")
        source_document = p.document(source)
        notes = document.get("terminal_notes") or source_document.get("review_notes", [])
        if terminal is None:
            if isinstance(request, CreateReview):
                inputs = document["intent"]["notes"]
                if not all(f"create-note-{index}" in document["steps"] for index in range(len(inputs))):
                    return None
                notes = [self.draft_receipt(document["steps"][f"create-note-{index}"]["response"], mr, note)
                         for index, note in enumerate(inputs)]
            if not notes:
                return None
            event = request.event if isinstance(request, (CreateReview, SubmitReview)) else None
            if event:
                if not all(f"publish-note-{note['id']}" in document["steps"] for note in notes):
                    return None
                if any(document["steps"][f"publish-note-{note['id']}"]["response"] not in ({}, None) for note in notes):
                    raise ToolFailed("The saved individual draft publication receipt is invalid.")
                if event == "REQUEST_CHANGES":
                    if "request-changes" not in document["steps"]:
                        return None
                    self.decision_receipt(document["steps"]["request-changes"]["response"], mr)
                state = "CHANGES_REQUESTED" if event == "REQUEST_CHANGES" else "COMMENTED"
                ids_key = "published_note_ids"
            elif isinstance(request, EditReview):
                key = f"edit-note-{request.note_id}"
                if key not in document["steps"]:
                    return None
                selected = next((note for note in notes if note["id"] == request.note_id), None)
                if selected is None:
                    raise ToolFailed("The edited note was not in the original task review.")
                updated = {**selected, "note": request.body}
                self.draft_receipt(document["steps"][key]["response"], mr, updated, request.note_id)
                notes = [updated if note["id"] == request.note_id else note for note in notes]
                state, ids_key = "PENDING", "draft_note_ids"
            elif isinstance(request, PendingReview):
                if not all(f"delete-note-{note['id']}" in document["steps"] for note in notes):
                    return None
                if any(document["steps"][f"delete-note-{note['id']}"]["response"] not in ({}, None) for note in notes):
                    raise ToolFailed("The saved individual draft deletion receipt is invalid.")
                state, ids_key = "DELETED", "draft_note_ids"
            else:
                state, ids_key = "PENDING", "draft_note_ids"
            terminal = {"review_id": source_id, **p.identity_scope, "number": mr.iid,
                        "head_sha": mr.sha, "state": state, ids_key: [note["id"] for note in notes]}
            await p.save(row, terminal_result=terminal, terminal_notes=notes)
        await p.save(source, review_state=terminal["state"], review_notes=notes)
        return await p.finish(row, terminal)

    async def publish(self, row: Operation, source: Operation, mr: MergeRequest, event: str) -> dict:
        p = self.p
        await self.claim(source, row)
        notes = p.document(source)["review_notes"]
        for note in notes:
            key = "publish-note-" + str(note["id"])
            if key not in p.document(row)["steps"]:
                await self.w.admitted_mr(row, mr.iid, mr.sha, mr.target_branch)
                await self.current_note(mr, note)
            value = await p.step(row, key, "PUT", p.prefix + f"/merge_requests/{mr.iid}/draft_notes/{note['id']}/publish", {})
            if value not in ({}, None):
                raise ToolFailed("GitLab did not acknowledge individual draft publication.")
        if event == "REQUEST_CHANGES":
            if "request-changes" not in p.document(row)["steps"]:
                await self.w.admitted_mr(row, mr.iid, mr.sha, mr.target_branch)
            await self.decision(row, mr)
        state = "CHANGES_REQUESTED" if event == "REQUEST_CHANGES" else "COMMENTED"
        result = {"review_id": source.id, **p.identity_scope, "number": mr.iid,
                  "head_sha": mr.sha, "state": state, "published_note_ids": [note["id"] for note in notes]}
        await p.save(row, terminal_result=result)
        await p.save(source, review_state=state)
        if row.id == source.id:
            row.document = source.document
        return result

    async def run(
        self, ctx: RunContext[RunScope], kind: str,
        request: CreateReview | PendingReview | SubmitReview | EditReview,
    ) -> dict:
        p = self.p
        async with p.attempt(ctx, kind, request) as row:
            if p.document(row).get("result"):
                return p.document(row)["result"]
            recovered = await self.recover(row, request)
            if recovered is not None:
                return recovered
            event = request.event if isinstance(request, (CreateReview, SubmitReview)) else None
            await self.require_decision(event)
            mr = await self.w.admitted_mr(row, request.number, request.head_sha, request.base_branch)
            if isinstance(request, CreateReview):
                notes = [{"note": request.body, "commit_id": mr.sha}] if request.body else []
                for comment in request.comments:
                    note: dict[str, Any] = {"note": comment.body, "commit_id": mr.sha}
                    if comment.position:
                        position = comment.position.model_dump(mode="json", exclude_none=True)
                        if mr.diff_refs is None or any(
                            position[key] != getattr(mr.diff_refs, key) for key in ("base_sha", "head_sha", "start_sha")
                        ):
                            raise ToolFailed("Inline review positions differ from the current MR diff references.")
                        note["position"] = position
                    notes.append(note)
                await p.freeze(row, mr=mr.model_dump(mode="json"), notes=notes, event=event)
                await p.save(row, review_id=row.id)
                saved = []
                for index, note in enumerate(notes):
                    key = "create-note-" + str(index)
                    if key not in p.document(row)["steps"]:
                        await self.w.admitted_mr(row, mr.iid, mr.sha, mr.target_branch)
                    value = await p.step(row, key, "POST", p.prefix + f"/merge_requests/{mr.iid}/draft_notes", note)
                    saved.append(self.draft_receipt(value, mr, note))
                    await p.save(row, review_notes=saved, review_state="PENDING")
                source = row
                result = (
                    await self.publish(row, source, mr, event) if event else
                    {"review_id": row.id, **p.identity_scope, "number": mr.iid, "head_sha": mr.sha,
                     "state": "PENDING", "draft_note_ids": [note["id"] for note in saved]}
                )
            else:
                source = await self.owned(request)
                await p.freeze(row, mr=mr.model_dump(mode="json"), review_id=source.id)
                await self.claim(source, row)
                if isinstance(request, SubmitReview):
                    result = await self.publish(row, source, mr, request.event)
                else:
                    notes = p.document(source)["review_notes"]
                    selected = [note for note in notes if note["id"] == request.note_id] if isinstance(request, EditReview) else notes
                    if not selected:
                        raise ToolFailed("This draft note is not owned by the selected task review.")
                    for note in selected:
                        key = ("edit-note-" if isinstance(request, EditReview) else "delete-note-") + str(note["id"])
                        if key not in p.document(row)["steps"]:
                            await self.w.admitted_mr(row, mr.iid, mr.sha, mr.target_branch)
                            await self.current_note(mr, note)
                        response = await p.step(
                            row, key, "PUT" if isinstance(request, EditReview) else "DELETE",
                            p.prefix + f"/merge_requests/{mr.iid}/draft_notes/{note['id']}",
                            {"note": request.body} if isinstance(request, EditReview) else {},
                        )
                        if isinstance(request, EditReview):
                            changed = {**note, "note": request.body}
                            self.draft_receipt(response, mr, changed, note["id"])
                            notes = [changed if item["id"] == note["id"] else item for item in notes]
                        elif response not in ({}, None):
                            raise ToolFailed("GitLab did not acknowledge deleting this task's pending draft note.")
                    state = "PENDING" if isinstance(request, EditReview) else "DELETED"
                    result = {"review_id": source.id, **p.identity_scope, "number": mr.iid,
                              "head_sha": mr.sha, "state": state, "draft_note_ids": [note["id"] for note in notes]}
                    await p.save(row, terminal_result=result, terminal_notes=notes)
                    await p.save(source, review_notes=notes, review_state=state)
            return await p.finish(row, result)
