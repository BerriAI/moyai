"""Typed GitLab wire shapes and safe canonical metadata, without workflow state."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

PositiveId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
AccessLevel = Annotated[int, Field(strict=True, ge=0, le=60)]
MAX_FILE_BYTES = 8 * 1024 * 1024
GIT_SHA = r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$'


def project_name(value: str) -> str:
    if (
        not isinstance(value, str) or len(value) > 1024
        or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', value)
        or any(part in {'.', '..'} or len(part) > 255 for part in value.split('/'))
    ):
        raise ValueError('Invalid GitLab project path.')
    return value


def path_component(value: str) -> str:
    """Encode a complete file path, branch, or ref as one GitLab path parameter."""
    if (
        not isinstance(value, str) or not value or len(value) > 4096
        or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in {'', '.', '..'} for part in value.split('/'))
    ):
        raise ValueError('Invalid GitLab path parameter.')
    return quote(value, safe='')


class GitLabResponse(BaseModel):
    # Providers add fields. Select known fields and never retain unknown secrets.
    model_config = ConfigDict(extra='ignore', strict=True, hide_input_in_errors=True)


class GitLabRepositoryOption(GitLabResponse):
    id: PositiveId
    full_name: str

    @field_validator('full_name')
    @classmethod
    def valid_name(cls, value: str) -> str:
        return project_name(value)


class GitLabRepositoryMetadata(GitLabRepositoryOption):
    path_with_namespace: str
    web_url: str
    default_branch: str | None
    visibility: Literal['private', 'internal', 'public']
    archived: bool
    access_level: AccessLevel


class GitLabCredentials(GitLabResponse):
    kind: Literal['personal']
    access_token: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    base_url: str = 'https://gitlab.com'
    repository_ids: list[PositiveId] = Field(min_length=1, max_length=1000)

    @field_validator('repository_ids')
    @classmethod
    def unique_ids(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(value))

    @field_validator('access_token')
    @classmethod
    def header_token(cls, value: SecretStr) -> SecretStr:
        if any(ord(c) < 33 or ord(c) > 126 for c in value.get_secret_value()):
            raise ValueError('Invalid GitLab access token.')
        return value


class GitLabUser(GitLabResponse):
    id: PositiveId
    username: str = Field(min_length=1, max_length=255, pattern=r'^[A-Za-z0-9_.-]+$')
    state: Literal['active']
    locked: bool = False


class GitLabPersonalAccessToken(GitLabResponse):
    id: PositiveId
    user_id: PositiveId
    scopes: list[str]
    active: bool
    revoked: bool


class GitLabProjectAccess(GitLabResponse):
    access_level: AccessLevel


class GitLabProjectPermissions(GitLabResponse):
    project_access: GitLabProjectAccess | None = None
    group_access: GitLabProjectAccess | None = None

    @property
    def effective_access_level(self) -> int:
        return max(
            self.project_access.access_level if self.project_access else 0,
            self.group_access.access_level if self.group_access else 0,
        )


class GitLabProject(GitLabResponse):
    id: PositiveId
    path_with_namespace: str
    default_branch: str | None = None
    visibility: Literal['private', 'internal', 'public']
    archived: bool
    permissions: GitLabProjectPermissions
    repository_access_level: Literal['disabled', 'private', 'enabled'] = 'enabled'
    merge_requests_access_level: Literal['disabled', 'private', 'enabled'] = 'enabled'
    merge_requests_enabled: bool = True

    @field_validator('path_with_namespace')
    @classmethod
    def valid_name(cls, value: str) -> str:
        return project_name(value)

    @field_validator('default_branch')
    @classmethod
    def valid_branch(cls, value: str | None) -> str | None:
        if value is not None:
            path_component(value)
        return value

    def metadata(self, base_url: str) -> GitLabRepositoryMetadata:
        return GitLabRepositoryMetadata(
            id=self.id, full_name=self.path_with_namespace,
            path_with_namespace=self.path_with_namespace,
            # Do not copy untrusted web/clone/import URLs from the provider.
            web_url=base_url + '/' + self.path_with_namespace,
            default_branch=self.default_branch, visibility=self.visibility,
            archived=self.archived, access_level=self.permissions.effective_access_level,
        )


class GitLabRepositoryFile(GitLabResponse):
    file_path: str
    encoding: Literal['base64']
    content: str = Field(max_length=4 * ((MAX_FILE_BYTES + 2) // 3), repr=False)
    content_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size: int = Field(ge=0)
    blob_id: str = Field(pattern=GIT_SHA)
    commit_id: str = Field(pattern=GIT_SHA)
    last_commit_id: str = Field(pattern=GIT_SHA)
    ref: str
    execute_filemode: bool = False

    def decoded_content(self, *, expected_path: str | None = None) -> bytes:
        if expected_path is not None and self.file_path != expected_path:
            raise ValueError('GitLab returned a different file path.')
        try:
            content = base64.b64decode(self.content, validate=True)
        except ValueError:
            raise ValueError('GitLab returned invalid base64 file content.') from None
        if (
            len(content) > MAX_FILE_BYTES
            or base64.b64encode(content).decode('ascii') != self.content
            or hashlib.sha256(content).hexdigest() != self.content_sha256
        ):
            raise ValueError('GitLab file content failed integrity validation.')
        # GitLab can convert text to UTF-8; size describes the original Git blob.
        return content


class GitLabDiffRefs(GitLabResponse):
    base_sha: str = Field(pattern=GIT_SHA)
    head_sha: str = Field(pattern=GIT_SHA)
    start_sha: str = Field(pattern=GIT_SHA)


class GitLabMergeRequest(GitLabResponse):
    id: PositiveId
    iid: PositiveId
    project_id: PositiveId
    source_project_id: PositiveId | None
    target_project_id: PositiveId
    source_branch: str
    target_branch: str
    title: str
    description: str | None = None
    state: Literal['opened', 'closed', 'merged', 'locked']
    draft: bool
    sha: str | None = Field(default=None, pattern=GIT_SHA)
    diff_refs: GitLabDiffRefs | None = None

    def check_identity(self, *, project_id: int, iid: int | None = None) -> None:
        if (
            self.project_id != project_id or self.target_project_id != project_id
            or (iid is not None and self.iid != iid)
        ):
            raise ValueError('GitLab returned a different merge request identity.')
