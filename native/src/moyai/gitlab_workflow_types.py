"""Finite native GitLab requests. Project IDs and MR iids are different identities."""

from __future__ import annotations

import base64
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from app.github import Change
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from .github_workflow_types import SHA, Branch, Identifier, RequestKey, branch_name

PROJECT_PATH = r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)+"


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"} or not parsed.hostname or "%" in parsed.netloc
        or any(ord(c) < 33 or c in "\\" for c in value)
        or not (parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
        ))
    ):
        raise ValueError("Use a fixed HTTPS GitLab origin (literal loopback HTTP is allowed for local testing).")
    try:
        _port = parsed.port
        if _port == 0:
            raise ValueError()
    except ValueError:
        raise ValueError("Invalid GitLab origin port.") from None
    return value.rstrip("/").lower()


def safe_text(value: str) -> str:
    # GitLab interprets quick-action lines even in descriptions and draft notes.
    # Reject every command-shaped line, including commands not currently known.
    if any(line.lstrip().startswith("/") for line in value.splitlines()) or "\x00" in value:
        raise ValueError("GitLab quick-action lines are unavailable in descriptions and review notes.")
    return value


Text = Annotated[str, Field(max_length=100_000), AfterValidator(safe_text)]
Title = Annotated[str, Field(min_length=3, max_length=250), AfterValidator(safe_text)]
Directory = Annotated[str, Field(pattern=r"^/workspace/[A-Za-z0-9_-][A-Za-z0-9_.-]*$", max_length=250)]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(Args):
    repository_id: Identifier | None = None
    repository: str = Field(default="", pattern="^(?:" + PROJECT_PATH + ")?$")


class Page(Target):
    page: int = Field(default=1, ge=1, le=10000)
    per_page: int = Field(default=30, ge=1, le=100)


class Write(Target):
    request_key: RequestKey


class Changes(Write):
    base_sha: SHA
    title: Title
    files: list[Change] | None = Field(default=None, max_length=1000)
    directory: Directory | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if (self.files is None) == (self.directory is None):
            raise ValueError("Choose files or a staged workspace directory.")
        if self.files is not None:
            paths = [item.path.lower() for item in self.files]
            if len(set(paths)) != len(paths):
                raise ValueError("Duplicate or case-colliding publication paths.")
            if any("/".join(path.split("/")[:i]) in paths for path in paths
                   for i in range(1, len(path.split("/")))):
                raise ValueError("A changed file cannot also be a parent directory.")
            total = sum(len(base64.b64decode(item.content or "")) if item.encoding == "base64"
                        else len((item.content or "").encode()) for item in self.files)
            if total > 20 * 1024 * 1024:
                raise ValueError("Publish at most 20 MiB of file contents.")
        return self


class CreateBranch(Write):
    branch: Branch
    commit_sha: SHA


class PublishBranch(Changes):
    branch: Branch
    expected_head_sha: SHA | None = None

    @model_validator(mode="after")
    def current_base(self) -> Self:
        if self.expected_head_sha and self.base_sha != self.expected_head_sha:
            raise ValueError("Existing branches require base_sha equal to expected_head_sha.")
        return self


class CreateMR(Changes):
    body: Text = Field(min_length=1)
    base_branch: Branch | None = None
    head_branch: Branch | None = None

    @field_validator("title")
    @classmethod
    def ready_title(cls, value: str) -> str:
        if re.match(r"(?i)^\s*(?:draft|wip)(?:\s*:|\s*\]|\s*\))|^\s*[\[(](?:draft|wip)[\])]", value):
            raise ValueError("Create a ready-for-review MR; draft titles are not permitted.")
        return value


class OpenMR(Write):
    base_branch: Branch
    head_branch: Branch
    expected_head_sha: SHA
    title: Title
    body: Text = Field(min_length=1)

    _ready = field_validator("title")(CreateMR.ready_title.__func__)

    @model_validator(mode="after")
    def different(self) -> Self:
        if self.base_branch == self.head_branch:
            raise ValueError("MR source and target branches must differ.")
        return self


class MRWrite(Write):
    number: Identifier = Field(description="Project-local GitLab MR iid, never its global id.")
    head_sha: SHA
    base_branch: Branch


class UpdateMR(Changes):
    number: Identifier
    base_branch: Branch


class RebaseMR(UpdateMR):
    expected_head_sha: SHA


class Retarget(MRWrite):
    target_branch: Branch


class Position(Args):
    position_type: Literal["text"] = "text"
    base_sha: SHA
    start_sha: SHA
    head_sha: SHA
    old_path: str = Field(min_length=1, max_length=4096)
    new_path: str = Field(min_length=1, max_length=4096)
    old_line: int | None = Field(default=None, ge=1)
    new_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def line(self) -> Self:
        if self.old_line is None and self.new_line is None:
            raise ValueError("An inline note requires an old or new line.")
        return self


class ReviewNote(Args):
    body: Text = Field(min_length=1)
    position: Position | None = None


class CreateReview(MRWrite):
    body: Text = ""
    comments: list[ReviewNote] = Field(default_factory=list, max_length=100)
    event: Literal["COMMENT", "REQUEST_CHANGES"] | None = None

    @model_validator(mode="after")
    def meaningful(self) -> Self:
        if not self.body.strip() and not self.comments:
            raise ValueError("Provide a review body or inline notes.")
        if self.event == "REQUEST_CHANGES" and not self.body.strip():
            raise ValueError("Requesting changes requires an explanation.")
        if any(note.position and note.position.head_sha != self.head_sha for note in self.comments):
            raise ValueError("Inline note positions must use the reviewed head.")
        return self


class PendingReview(MRWrite):
    review_id: str = Field(pattern=r"^[0-9a-f]{64}$", description="Moyai review operation identity.")


class SubmitReview(PendingReview):
    event: Literal["COMMENT", "REQUEST_CHANGES"]


class EditReview(PendingReview):
    note_id: Identifier
    body: Text = Field(min_length=1)


class Checkout(Target):
    directory: Directory
    ref: str | None = Field(default=None, min_length=1, max_length=1024)
    number: Identifier | None = None

    @field_validator("ref")
    @classmethod
    def valid_ref(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{40}", value):
            branch_name(value)
        return value

    @model_validator(mode="after")
    def source(self) -> Self:
        if self.ref is not None and self.number is not None:
            raise ValueError("Choose a branch/SHA or MR iid.")
        if str(PurePosixPath(self.directory)) != self.directory:
            raise ValueError("Use a canonical workspace path.")
        return self


class Status(Write):
    operation: Literal[
        "create_branch", "publish_branch", "create_merge_request", "open_merge_request",
        "update_merge_request", "rebase_merge_request", "close_merge_request", "reopen_merge_request",
        "retarget_merge_request", "convert_merge_request_to_draft", "request_merge_request_write_access",
        "request_mr_lifecycle_access", "create_review", "submit_review", "edit_pending_review",
        "delete_pending_review", "create_stack", "add_to_stack",
    ]


class ProviderObject(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class Project(ProviderObject):
    id: Identifier
    path_with_namespace: str = Field(pattern="^" + PROJECT_PATH + "$")
    default_branch: Branch
    http_url_to_repo: str
    web_url: str


class Commit(ProviderObject):
    id: SHA


class BranchInfo(ProviderObject):
    name: Branch
    protected: bool
    commit: Commit


class DiffRefs(ProviderObject):
    base_sha: SHA
    head_sha: SHA
    start_sha: SHA


class MergeRequest(ProviderObject):
    id: Identifier
    iid: Identifier
    project_id: Identifier
    source_project_id: Identifier
    target_project_id: Identifier
    source_branch: Branch
    target_branch: Branch
    sha: SHA
    state: Literal["opened", "closed", "merged", "locked"]
    draft: bool
    title: str
    web_url: str
    diff_refs: DiffRefs | None = None
    merged_at: str | None = None


class StackMember(Args):
    number: Identifier
    head_sha: SHA
    head_branch: Branch
    base_branch: Branch


class CreateStack(Write):
    merge_requests: list[StackMember] = Field(min_length=2, max_length=100)


class StackRead(Target):
    stack_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class AddToStack(CreateStack):
    merge_requests: list[StackMember] = Field(min_length=1, max_length=99)
    stack_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    members: list[StackMember] = Field(min_length=2, max_length=99)


class RefRead(Page):
    ref: str = Field(default="HEAD", min_length=1, max_length=1024)


class Tree(RefRead):
    path: str = Field(default="", max_length=4096)
    recursive: bool = False


class File(RefRead):
    path: str = Field(min_length=1, max_length=4096)


class Compare(Target):
    base_sha: SHA
    head_sha: SHA


class Search(Page):
    scope: Literal["blobs", "commits", "issues", "merge_requests", "notes"]
    query: str = Field(min_length=1, max_length=1000)
    ref: str | None = Field(default=None, min_length=1, max_length=1024)


class Item(Page):
    number: Identifier


class MRRead(Item):
    head_sha: SHA | None = None
    base_sha: SHA | None = None


class Pipelines(Page):
    sha: SHA


class PipelineJobs(Page):
    pipeline_id: Identifier
    sha: SHA


class Job(Target):
    job_id: Identifier
    sha: SHA


class Log(Job):
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=1000)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Artifact(Job):
    path: str = Field(min_length=1, max_length=4096)
