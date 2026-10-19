"""Validated inputs and provider identities for native GitHub workflows."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from app.github import Change, Changes, Repository
from app.github import Publish as FilePublication
from app.github_coding import InlineComment
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def branch_name(value: str) -> str:
    if (
        value in {"@", "HEAD"} or value.startswith("-") or value.endswith(".")
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127
               or character in "~^:?*[\\" for character in value)
        or ".." in value or "@{" in value
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    ):
        raise ValueError("Use a Git branch name, not a ref expression.")
    return value


def nonzero_sha(value: str) -> str:
    if value == "0" * 40:
        raise ValueError("A commit cannot be the zero object ID.")
    return value


Branch = Annotated[str, Field(min_length=1, max_length=1024), AfterValidator(branch_name)]
SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True), AfterValidator(nonzero_sha)]
Identifier = Annotated[int, Field(gt=0, strict=True)]
RequestKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")]
ReviewEvent = Literal["COMMENT", "REQUEST_CHANGES"]


class Publish(FilePublication):
    base_sha: SHA
    base_branch: Branch | None = None
    head_branch: Branch | None = None


class Write(Repository):
    request_key: RequestKey


class CreateBranch(Write):
    branch: Branch
    commit_sha: SHA


class PublishBranch(Changes):
    base_sha: SHA
    branch: Branch
    title: str = Field(min_length=3, max_length=250)
    expected_head_sha: SHA | None = None

    @model_validator(mode="after")
    def update_from_current_head(self) -> Self:
        if self.expected_head_sha is not None and self.base_sha != self.expected_head_sha:
            raise ValueError("Branch updates must prepare files against the exact expected head.")
        return self


class WorkspacePublishBranch(Write):
    """Publish the staged tree without copying file contents through the model."""

    directory: str = Field(pattern=r"^/workspace/[A-Za-z0-9_.-]+$", max_length=250)
    base_sha: SHA
    branch: Branch
    title: str = Field(min_length=3, max_length=250)
    expected_head_sha: SHA | None = None


class OpenPullRequest(Write):
    base_branch: Branch
    head_branch: Branch
    expected_head_sha: SHA
    title: str = Field(min_length=3, max_length=250)
    body: str = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def different_refs(self) -> Self:
        if self.base_branch == self.head_branch:
            raise ValueError("The PR base and head must differ.")
        return self


class Rebase(Changes):
    base_sha: SHA
    number: Identifier
    expected_head_sha: SHA
    base_branch: Branch
    title: str = Field(min_length=3, max_length=250)
    files: list[Change] = Field(description="Complete prepared changes relative to base_sha; [] resets to its tree.")


class PRWrite(Write):
    number: Identifier
    head_sha: SHA


class LifecycleAccess(PRWrite):
    """Consent is for this PR's metadata during this task, including later commits."""


class Retarget(PRWrite):
    base_branch: Branch


class CreateReview(PRWrite):
    body: str = Field(default="", max_length=100_000)
    comments: list[InlineComment] = Field(default_factory=list, max_length=100)
    event: ReviewEvent | None = Field(
        default=None, description="Omit to prepare a pending review. Approval is unavailable.",
    )

    @model_validator(mode="after")
    def changes_need_explanation(self) -> Self:
        if self.event == "REQUEST_CHANGES" and not self.body.strip():
            raise ValueError("Requesting changes requires a review body.")
        return self


class PendingReview(PRWrite):
    review_id: Identifier


class SubmitReview(PendingReview):
    event: ReviewEvent
    body: str = Field(default="", max_length=100_000)

    @model_validator(mode="after")
    def changes_need_explanation(self) -> Self:
        if self.event == "REQUEST_CHANGES" and not self.body.strip():
            raise ValueError("Requesting changes requires a review body.")
        return self


class EditReview(PendingReview):
    body: str = Field(min_length=1, max_length=100_000)


class Checkout(Repository):
    directory: str = Field(pattern=r"^/workspace/[A-Za-z0-9_.-]+$", max_length=250)
    ref: str | None = Field(default=None, min_length=1, max_length=1024)
    number: Identifier | None = None

    @field_validator("directory")
    @classmethod
    def safe_directory(cls, value: str) -> str:
        path = PurePosixPath(value)
        if str(path) != value or any(part in {".", "..", ".git"} for part in path.parts):
            raise ValueError("Choose a fresh directory beneath /workspace.")
        return value

    @field_validator("ref")
    @classmethod
    def safe_ref(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{40}", value):
            return branch_name(value)
        return value

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if self.ref is not None and self.number is not None:
            raise ValueError("Choose either a ref or a PR number.")
        return self


class PublicationStatus(Write):
    operation: Literal[
        "create_pull_request", "update_pull_request", "create_branch", "publish_branch",
        "open_pull_request", "rebase_pull_request",
        "close_pull_request", "reopen_pull_request", "retarget_pull_request", "convert_pull_request_to_draft",
        "create_review", "submit_review", "edit_pending_review", "delete_pending_review",
        "create_stack", "add_to_stack",
    ] = Field(
        default="create_pull_request",
        description="Original unprefixed write-tool name, such as update_pull_request for a follow-up. "
                    "Use that operation's unchanged request_key; this lookup never repeats a provider write.",
    )


class ProviderObject(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class AuthorityRef(ProviderObject):
    model_config = ConfigDict(extra="ignore", strict=True, populate_by_name=True)
    id: str | None = Field(default=None, alias="_authority_id", pattern=r"^[0-9a-f]{64}$")
    legacy_owned: bool = Field(default=False, alias="_legacy_owned")

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if (self.id is None) != self.legacy_owned:
            raise ValueError("Authority requires one native operation or a verified historical creation.")
        return self


class DispatchAuthority(AuthorityRef):
    number: Identifier
    head: SHA
    base: Branch
    lifecycle: bool = False


class RepositoryIdentity(ProviderObject):
    id: Identifier
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    default_branch: Branch
    node_id: str = Field(min_length=1)


class ReferenceRepository(ProviderObject):
    id: Identifier


class Reference(ProviderObject):
    ref: Branch
    sha: SHA
    repo: ReferenceRepository


class PullRequest(ProviderObject):
    number: Identifier
    node_id: str = Field(min_length=1)
    head: Reference
    base: Reference
    state: Literal["open", "closed"]
    draft: bool
    merged_at: str | None = None
    merged: bool = False
    html_url: str = Field(pattern=r"^https://github\.com/")


class Commit(ProviderObject):
    sha: SHA


class GitObject(ProviderObject):
    type: Literal["commit"]
    sha: SHA


class GitReference(ProviderObject):
    ref: str
    object: GitObject


class Review(ProviderObject):
    id: Identifier
    commit_id: SHA
    state: Literal["PENDING", "COMMENTED", "CHANGES_REQUESTED", "APPROVED", "DISMISSED"]
    body: str


class GraphQLError(ProviderObject):
    message: str


class DraftReceipt(ProviderObject):
    number: Identifier
    isDraft: bool
    url: str


class DraftMutation(ProviderObject):
    pullRequest: DraftReceipt | None = None


class DraftData(ProviderObject):
    convertPullRequestToDraft: DraftMutation | None = None


class DraftResponse(ProviderObject):
    data: DraftData | None = None
    errors: list[GraphQLError] = Field(default_factory=list)


class RebaseMutation(ProviderObject):
    clientMutationId: str | None = None


class RebaseData(ProviderObject):
    updateRefs: RebaseMutation | None = None


class RebaseResponse(ProviderObject):
    data: RebaseData | None = None
    errors: list[GraphQLError] = Field(default_factory=list)


class StackMember(ProviderObject):
    number: Identifier
    head: Reference
    base: Reference
    state: Literal["open", "closed"]
    draft: bool
    merged_at: str | None


class StackBase(ProviderObject):
    ref: Branch


class Stack(ProviderObject):
    id: Identifier
    number: Identifier
    node_id: str
    base: StackBase
    open: bool
    pull_requests: list[StackMember] = Field(max_length=100)


class StackList(Repository):
    pull_request: Identifier | None = None
    page: int = Field(default=1, ge=1, le=10000)
    per_page: int = Field(default=30, ge=1, le=100)


class StackRead(Repository):
    stack_number: Identifier


class CreateStack(Write):
    pull_requests: list[StackMember] = Field(min_length=2, max_length=100)


class ExistingStack(Write):
    stack_number: Identifier
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    members: list[StackMember] = Field(min_length=1, max_length=100)


class AddToStack(ExistingStack):
    pull_requests: list[StackMember] = Field(min_length=1, max_length=99)
