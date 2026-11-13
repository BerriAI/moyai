"""The two fixed review GraphQL operations; never a general query transport."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .connector_errors import ConnectorError
from .gitlab_schemas import GIT_SHA, PositiveId, project_name
from .gitlab_transport import preflight_error

REQUEST_CHANGES = """mutation MoyaiRequestChanges($projectPath:ID!,$iid:String!){
  mergeRequestRequestChanges(input:{projectPath:$projectPath,iid:$iid}){
    errors mergeRequest{id iid projectId sourceProjectId targetProjectId diffHeadSha}}}"""

REVIEW_CAPABILITY = """query MoyaiReviewCapability{
  __type(name:"Mutation"){fields{name}}}"""

MAX_SCHEMA_RESPONSE_BYTES = 16 * 1024 * 1024


class GitLabReviewVariables(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    project_path: str = Field(alias='projectPath')
    iid: str = Field(pattern=r'^[1-9][0-9]{0,18}$')

    @field_validator('project_path')
    @classmethod
    def valid_project_path(cls, value: str) -> str:
        return project_name(value)

    @field_validator('iid')
    @classmethod
    def valid_iid(cls, value: str) -> str:
        if int(value) > 2**63 - 1:
            raise ValueError('Invalid GitLab merge request IID.')
        return value


def review_variables(payload: Mapping[str, Any] | None) -> GitLabReviewVariables | None:
    """None identifies the one fixed capability query; reject every other shape."""
    if payload == {'query': REVIEW_CAPABILITY}:
        return None
    if (
        isinstance(payload, Mapping) and set(payload) == {'query', 'variables'}
        and payload['query'] == REQUEST_CHANGES
    ):
        try:
            return GitLabReviewVariables.model_validate(payload['variables'])
        except ValidationError:
            pass
    raise preflight_error(
        'Only the fixed GitLab review capability query and request-changes mutation are allowed.',
        'gitlab_invalid_graphql',
    ) from None


class _Response(BaseModel):
    model_config = ConfigDict(extra='ignore', strict=True, hide_input_in_errors=True)


class _CapabilityField(_Response):
    name: str = Field(pattern=r'^[_A-Za-z][_0-9A-Za-z]*$', max_length=128)


class _MutationType(_Response):
    fields: list[_CapabilityField] = Field(max_length=4096)


class _CapabilityData(_Response):
    mutation_type: _MutationType | None = Field(alias='__type')


class _CapabilityResponse(_Response):
    data: _CapabilityData


class _ReviewedMergeRequest(_Response):
    id: str = Field(pattern=r'^gid://gitlab/MergeRequest/[1-9][0-9]{0,18}$')
    iid: str = Field(pattern=r'^[1-9][0-9]{0,18}$')
    project_id: PositiveId = Field(alias='projectId')
    source_project_id: PositiveId = Field(alias='sourceProjectId')
    target_project_id: PositiveId = Field(alias='targetProjectId')
    head_sha: str = Field(alias='diffHeadSha', pattern=GIT_SHA)


class _RequestChanges(_Response):
    errors: list[str] = Field(max_length=0)
    merge_request: _ReviewedMergeRequest = Field(alias='mergeRequest')


class _RequestChangesData(_Response):
    request_changes: _RequestChanges = Field(alias='mergeRequestRequestChanges')


class _RequestChangesResponse(_Response):
    data: _RequestChangesData


def _capability_projection(value: object) -> object:
    """Production GitLab returns its full static schema for any introspection.

    Only Mutation field names leave this boundary; descriptions, other type
    definitions, directives and extensions are never returned to native tools.
    """
    if not isinstance(value, Mapping) or not isinstance(value.get('data'), Mapping):
        return value
    data = value['data']
    if '__schema' not in data:
        return value
    schema = data['__schema']
    if not isinstance(schema, Mapping):
        raise TypeError
    root = schema.get('mutationType')
    types = schema.get('types')
    if (
        not isinstance(root, Mapping) or root.get('name') != 'Mutation'
        or not isinstance(types, list) or len(types) > 12000
    ):
        raise ValueError
    matches = [item for item in types if isinstance(item, Mapping) and item.get('name') == 'Mutation']
    if len(matches) != 1 or matches[0].get('kind') != 'OBJECT':
        raise ValueError
    return {'data': {'__type': {'fields': matches[0].get('fields')}}}


def review_response(
    value: object, *, project_id: int | None, variables: GitLabReviewVariables | None,
) -> dict[str, Any]:
    """Allowlist output and never expose GraphQL errors or success-shaped failures."""
    if isinstance(value, Mapping) and value.get('errors'):
        raise ConnectorError(
            'GitLab returned GraphQL errors. Verify the review state before retrying a mutation.',
            code='gitlab_graphql_errors', dispatched=True,
        )
    try:
        if variables is None:
            return _CapabilityResponse.model_validate(_capability_projection(value)).model_dump(by_alias=True)
        result = _RequestChangesResponse.model_validate(value)
        changed = result.data.request_changes.merge_request
        if (
            changed.project_id != project_id or changed.source_project_id != project_id
            or changed.target_project_id != project_id or changed.iid != variables.iid
        ):
            raise ValueError
        return result.model_dump(by_alias=True)
    except (ValidationError, ValueError, TypeError):
        raise ConnectorError(
            'GitLab did not confirm the expected review operation and project identity. '
            'Verify the review state before retrying a mutation.',
            code='gitlab_invalid_graphql_response', dispatched=True,
        ) from None
