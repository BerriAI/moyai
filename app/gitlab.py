"""GitLab REST boundary and canonical setup metadata.

Native workflows own grants, revision fences, effects and write policy. This is
trusted server code, never an agent-facing arbitrary REST proxy.
"""

from __future__ import annotations

import asyncio
import hmac
import json as jsonlib
import math
import re
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import JsonValue, ValidationError

from .connector_errors import ConnectorError
from .gitlab_graphql import (
    MAX_SCHEMA_RESPONSE_BYTES,
    review_response,
    review_variables,
)
from .gitlab_schemas import (
    GitLabCredentials,
    GitLabPersonalAccessToken,
    GitLabProject,
    GitLabRepositoryOption,
    GitLabUser,
    path_component,
)
from .gitlab_text import safe_text
from .gitlab_transport import (
    DEFAULT_BASE_URL,
    GitLabTransport,
    normalize_base_url,
    preflight_error,
    validate_path,
)

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024 * 1024
_AUTH_FIELDS = {'access_token', 'private_token', 'job_token', 'sudo', 'authorization', '_method'}
__all__ = ['GitLab', 'normalize_base_url']


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _write_text(value: object) -> None:
    """Quick actions can merge/approve through otherwise harmless note writes."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                isinstance(key, str) and key.partition('[')[0] in {'body', 'note', 'description'}
                and any(
                    _unsafe_text(text)
                    for text in (item if isinstance(item, (list, tuple)) else [item])
                    if isinstance(text, str)
                )
            ):
                raise preflight_error(
                    'GitLab quick actions are not allowed in notes or descriptions.',
                    'gitlab_quick_action',
                )
            _write_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _write_text(item)


def _unsafe_text(value: str) -> bool:
    try:
        safe_text(value)
    except ValueError:
        return True
    return False


def _validate_arguments(
    method: str, path: str, params: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None, missing: bool,
) -> None:
    if method not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'}:
        raise preflight_error('Unsupported GitLab HTTP method.')
    if missing and method not in {'GET', 'HEAD'}:
        raise preflight_error('Only GitLab reads can treat a missing resource as empty.')
    if path in {'/user', '/personal_access_tokens/self'} and method not in {'GET', 'HEAD'}:
        raise preflight_error('GitLab identity endpoints are read-only.')
    for value in (params, payload):
        if value is not None and not isinstance(value, Mapping):
            raise preflight_error('GitLab parameters and JSON payloads must be objects.')
        if value is not None and any(
            not isinstance(key, str) or key.partition('[')[0].casefold() in _AUTH_FIELDS
            for key in value
        ):
            raise preflight_error('GitLab authentication or method overrides are not allowed.')
    try:
        if params is not None and len(str(httpx.QueryParams(params))) > 16384:
            raise ValueError
        if payload is not None:
            if len(jsonlib.dumps(payload, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
                raise ValueError
            if method in {'GET', 'HEAD'}:
                raise preflight_error('GitLab reads cannot include a mutation body.')
            _write_text(payload)
        if params is not None and method not in {'GET', 'HEAD'}:
            _write_text(params)
    except (ValueError, TypeError, RecursionError):
        raise preflight_error('GitLab request parameters are invalid or too large.') from None


def _validate_discovery(
    method: str, params: Mapping[str, Any] | None, payload: Mapping[str, Any] | None,
    *, missing: bool, raw: bool,
) -> None:
    """Only the bounded membership discovery shape used by admin setup routes."""
    required = {'membership', 'simple', 'per_page', 'page', 'order_by', 'sort'}
    if (
        method != 'GET' or raw or missing or payload is not None or not isinstance(params, Mapping)
        or not required <= set(params) or set(params) - required - {'search'}
        or params.get('membership') is not True or params.get('simple') is not True
        or params.get('order_by') != 'id' or params.get('sort') != 'asc'
        or type(params.get('page')) is not int or not 1 <= params['page'] <= 10000
        or type(params.get('per_page')) is not int or not 1 <= params['per_page'] <= 100
        or not isinstance(params.get('search', ''), str) or len(params.get('search', '')) > 200
    ):
        raise preflight_error(
            'GitLab project discovery requires a bounded, membership-only admin setup query.',
            'gitlab_invalid_discovery',
        )


def _retry_after(headers: httpx.Headers) -> float | None:
    try:
        value = float(headers.get('retry-after', ''))
        return value if math.isfinite(value) and 0 <= value <= 86400 else None
    except ValueError:
        return None


class GitLab:
    def __init__(self, store: Any, security: Any, settings: Any, connectors: Any):
        self.store, self.security, self.settings, self.connectors = store, security, settings, connectors

    @property
    def allow_loopback_http(self) -> bool:
        return getattr(self.settings, 'gitlab_allow_loopback_http', False) is True

    def normalize_base_url(self, value: str = DEFAULT_BASE_URL) -> str:
        return normalize_base_url(value, allow_loopback_http=self.allow_loopback_http)

    def saved_credentials(self, *, connection: Any = None) -> dict[str, Any]:
        rows = self.store.rows(
            "SELECT encrypted FROM connections WHERE provider='gitlab'", connection=connection,
        )
        if not rows:
            return {}
        try:
            credentials = jsonlib.loads(self.security.decrypt(rows[0]['encrypted']))
            if not isinstance(credentials, dict):
                raise TypeError
            return credentials
        except (ValueError, TypeError):
            raise preflight_error('Reconnect GitLab to restore valid saved credentials.') from None

    def _credentials(self, credentials: dict[str, Any]) -> GitLabCredentials:
        try:
            parsed = GitLabCredentials.model_validate(credentials)
        except ValidationError:
            raise preflight_error(
                'Provide a GitLab personal access token and selected numeric project IDs.',
                'gitlab_invalid_credentials',
            ) from None
        parsed.base_url = self.normalize_base_url(parsed.base_url)
        return parsed

    def connected_ids(self, credentials: dict[str, Any]) -> list[int]:
        return self._credentials(credentials).repository_ids

    def connected_targets(self, credentials: dict[str, Any]) -> list[int]:
        return self.connected_ids(credentials)

    def repository_options(
        self, credentials: dict[str, Any] | None = None, *, connection: Any = None,
    ) -> list[dict[str, Any]]:
        credentials = credentials if credentials is not None else self.saved_credentials(connection=connection)
        if not credentials:
            return []
        ids = self.connected_ids(credentials)
        by_id: dict[int, GitLabRepositoryOption] = {}
        repositories = credentials.get('repositories', [])
        if not isinstance(repositories, list):
            raise preflight_error('Refresh GitLab to restore selected project metadata.')
        try:
            for value in repositories:
                item = GitLabRepositoryOption.model_validate(value)
                if item.id in by_id and by_id[item.id] != item:
                    raise ValueError
                by_id[item.id] = item
        except (ValidationError, ValueError):
            raise preflight_error('Refresh GitLab to restore selected project metadata.') from None
        return [by_id[identity].model_dump() for identity in ids if identity in by_id]

    def targets(self) -> list[str]:
        return [item['full_name'] for item in self.repository_options()]

    def target(
        self, repository: str | int, credentials: dict[str, Any] | None = None,
        *, connection: Any = None,
    ) -> int:
        credentials = credentials if credentials is not None else self.saved_credentials(connection=connection)
        ids = self.connected_ids(credentials)
        if type(repository) is int and repository in ids:
            return repository
        if isinstance(repository, str):
            if re.fullmatch(r'[1-9][0-9]{0,18}', repository) and int(repository) in ids:
                return int(repository)
            matches = [
                item['id'] for item in self.repository_options(credentials)
                if item['full_name'].casefold() == repository.casefold()
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise preflight_error('This project name is ambiguous. Choose its permanent GitLab ID.')
        raise preflight_error(
            'Select an explicit project ID enabled in the saved GitLab connection.',
            'gitlab_project_not_selected',
        )

    def project_path(self, repository: str | int, suffix: str = '', *, credentials=None) -> str:
        """Construct a project path only after resolving saved selected identity."""
        if not isinstance(suffix, str) or (suffix and not suffix.startswith('/')):
            raise preflight_error('A GitLab project endpoint suffix must begin with a slash.')
        path = f'/projects/{self.target(repository, credentials)}' + suffix
        return validate_path(path)

    def file_path(self, repository: str | int, file_path: str, *, credentials=None) -> str:
        try:
            component = path_component(file_path)
        except ValueError:
            raise preflight_error('Use a repository-relative GitLab file path.') from None
        return self.project_path(repository, '/repository/files/' + component, credentials=credentials)

    async def verify(self, credentials: dict[str, Any]) -> str:
        parsed = self._credentials(credentials)
        token = parsed.access_token.get_secret_value()
        try:
            user = GitLabUser.model_validate(
                await self.request('GET', '/user', token=token, base_url=parsed.base_url),
            )
            if user.locked:
                raise preflight_error('The GitLab account is locked.', 'gitlab_access_denied')
            personal = GitLabPersonalAccessToken.model_validate(
                await self.request('GET', '/personal_access_tokens/self', token=token, base_url=parsed.base_url),
            )
            if (
                personal.user_id != user.id or not personal.active or personal.revoked
                or 'api' not in personal.scopes
            ):
                raise preflight_error(
                    'Use an active personal access token with the GitLab api scope for this account.',
                    'gitlab_access_denied',
                )
            repositories = []
            for identity in parsed.repository_ids:
                project = GitLabProject.model_validate(
                    await self.request('GET', f'/projects/{identity}', token=token, base_url=parsed.base_url),
                )
                if project.id != identity:
                    raise preflight_error('GitLab returned a different selected project ID.', 'gitlab_identity_mismatch')
                if (
                    project.archived or project.permissions.effective_access_level < 30
                    or project.repository_access_level == 'disabled'
                    or project.merge_requests_access_level == 'disabled'
                    or not project.merge_requests_enabled
                ):
                    raise preflight_error(
                        'Selected GitLab projects require Developer or higher access, '
                        'enabled repositories and merge requests, and must not be archived.',
                        'gitlab_access_denied',
                    )
                repositories.append(project.metadata(parsed.base_url).model_dump())
            if len({item['full_name'].casefold() for item in repositories}) != len(repositories):
                raise preflight_error('GitLab returned conflicting project identities.', 'gitlab_identity_mismatch')
        except ValidationError:
            raise preflight_error(
                'GitLab did not confirm the account, token permissions, or selected project metadata.',
                'gitlab_invalid_response',
            ) from None
        # One atomic in-memory update, only after every selected project passes.
        # The caller owns encrypted persistence, revision fences and auditing.
        credentials.update(
            kind='personal', base_url=parsed.base_url, repository_ids=parsed.repository_ids,
            repositories=repositories, user_id=user.id, username=user.username,
        )
        return ', '.join(item['full_name'] for item in repositories)

    async def request(
        self, method: str, path: str, *, token: str, base_url: str = DEFAULT_BASE_URL,
        params: Mapping[str, Any] | None = None, json: Mapping[str, Any] | None = None,
        missing: bool = False, raw: bool = False,
    ) -> JsonValue | bytes:
        data, _headers = await self._request(
            method, path, token=token, base_url=base_url, params=params,
            json=json, missing=missing, raw=raw,
        )
        return data

    async def _request(
        self, method: str, path: str, *, token: str, base_url: str,
        params: Mapping[str, Any] | None = None, json: Mapping[str, Any] | None = None,
        missing: bool = False, raw: bool = False,
    ) -> tuple[JsonValue | bytes, httpx.Headers]:
        origin = self.normalize_base_url(base_url)
        path = validate_path(path)
        if not isinstance(method, str):
            raise preflight_error('Unsupported GitLab HTTP method.')
        method = method.upper()
        _validate_arguments(method, path, params, json, missing)
        if (
            not isinstance(token, str) or not token or len(token) > 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise preflight_error('Provide a valid GitLab access token.', 'gitlab_invalid_credentials')
        graphql = path == '/graphql'
        variables, graphql_project = None, None
        if graphql:
            if method != 'POST' or params is not None or raw or missing:
                raise preflight_error('Use only the fixed GitLab review GraphQL operation.', 'gitlab_invalid_graphql')
            variables = review_variables(json)
            credentials = self.saved_credentials()
            current = self._credentials(credentials)
            if (
                current.base_url != origin
                or not hmac.compare_digest(current.access_token.get_secret_value(), token)
            ):
                raise preflight_error(
                    'The saved GitLab connection changed. Reload it before requesting a review operation.',
                    'gitlab_connection_changed',
                )
            if variables is not None:
                graphql_project = self.target(variables.project_path, credentials)
        elif path == '/projects':
            _validate_discovery(method, params, json, missing=missing, raw=raw)
        response_limit = MAX_SCHEMA_RESPONSE_BYTES if graphql and variables is None else MAX_RESPONSE_BYTES
        headers = {
            **({'Authorization': 'Bearer ' + token} if graphql else {'PRIVATE-TOKEN': token}),
            'Accept': '*/*' if raw else 'application/json',
            'Accept-Encoding': 'identity',
        }
        dispatched = False
        try:
            async with asyncio.timeout(60), httpx.AsyncClient(
                transport=GitLabTransport(allow_loopback_http=self.allow_loopback_http),
                timeout=httpx.Timeout(45, connect=10), follow_redirects=False, trust_env=False,
            ) as client:
                # Build before marking dispatch so invalid inputs cannot become
                # uncertain writes. No headers, body text, or causes enter errors.
                request = client.build_request(
                    method, origin + ('/api/graphql' if graphql else '/api/v4' + path),
                    headers=headers, params=params, json=json,
                )
                dispatched = True
                response = await client.send(request, stream=True)
                try:
                    if missing and response.status_code == 404:
                        return None, response.headers
                    if not 200 <= response.status_code < 300:
                        code = (
                            'rate_limited' if response.status_code == 429 else
                            'access_denied' if response.status_code in {401, 403} else
                            'gitlab_redirect_refused' if 300 <= response.status_code < 400 else
                            'provider_http_error'
                        )
                        raise ConnectorError(
                            f'GitLab did not confirm the operation ({response.status_code}). '
                            'Check access and the destination before retrying a write.',
                            code=code, dispatched=True,
                            rejected=400 <= response.status_code < 500 and response.status_code != 408,
                            status_code=response.status_code,
                            retryable=response.status_code == 429 or response.status_code >= 500,
                            retry_after=_retry_after(response.headers),
                        )
                    length = response.headers.get('content-length')
                    if method != 'HEAD' and length and (
                        not length.isascii() or not length.isdigit() or int(length) > response_limit
                    ):
                        raise ConnectorError(
                            'GitLab response exceeded the supported size or had an invalid length.',
                            code='gitlab_response_too_large', dispatched=True,
                        )
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise ConnectorError(
                            'GitLab returned an unexpected compressed response.',
                            code='gitlab_invalid_response', dispatched=True,
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(content) + len(chunk) > response_limit:
                            raise ConnectorError(
                                'GitLab response exceeded the supported size. Narrow the requested page or file.',
                                code='gitlab_response_too_large', dispatched=True,
                            )
                        content.extend(chunk)
                    if raw:
                        return bytes(content), response.headers
                    if not content and (response.status_code == 204 or method == 'HEAD'):
                        if graphql or path == '/projects':
                            raise ValueError
                        return {}, response.headers
                    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if content_type != 'application/json' and not content_type.endswith('+json'):
                        raise ValueError
                    result = jsonlib.loads(
                        content, parse_constant=_reject_json_constant, parse_float=_finite_json_float,
                    )
                    if graphql:
                        result = review_response(result, project_id=graphql_project, variables=variables)
                    elif path == '/projects' and (
                        not isinstance(result, list) or params is None or len(result) > params['per_page']
                    ):
                        raise ConnectorError(
                            'GitLab returned an invalid project discovery page.',
                            code='gitlab_invalid_response', dispatched=True,
                        )
                    return result, response.headers
                finally:
                    await response.aclose()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise ConnectorError(
                'Could not connect to GitLab. No request was sent.',
                code='connection_unavailable', dispatched=False, retryable=True,
            ) from None
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, RecursionError):
            raise ConnectorError(
                'The GitLab response was not received or could not be decoded. '
                'Verify the destination before retrying a write.',
                code='response_unavailable', dispatched=dispatched, retryable=True,
            ) from None

    async def page(
        self, path: str, *, token: str, base_url: str = DEFAULT_BASE_URL,
        params: Mapping[str, Any] | None = None, page: int = 1, per_page: int = 100,
    ) -> dict[str, Any]:
        """One bounded offset page. Never follow provider Link/Location URLs.

        Some GitLab endpoints omit pagination headers. A full page without a
        header requires one more numeric page; it never implies complete coverage.
        """
        if type(page) is not int or not 1 <= page <= 2**31 - 2 or type(per_page) is not int or not 1 <= per_page <= 100:
            raise preflight_error('Use a positive GitLab page and a per_page limit from 1 to 100.')
        query = dict(params or {})
        if any(key in query for key in ('page', 'per_page', 'pagination', 'page_token')):
            raise preflight_error('Pass offset pagination through page and per_page only.')
        data, headers = await self._request(
            'GET', path, token=token, base_url=base_url,
            params={**query, 'page': page, 'per_page': per_page},
        )
        if not isinstance(data, list) or len(data) > per_page:
            raise ConnectorError('GitLab returned an invalid page.', code='gitlab_invalid_response', dispatched=True)
        next_page = None
        if 'x-next-page' in headers:
            value = headers['x-next-page']
            if value:
                if not re.fullmatch(r'[1-9][0-9]{0,9}', value) or int(value) != page + 1:
                    raise ConnectorError(
                        'GitLab returned an invalid page continuation.',
                        code='gitlab_invalid_response', dispatched=True,
                    )
                next_page = int(value)
        elif len(data) == per_page:
            next_page = page + 1
        return {'items': data, 'page': page, 'per_page': per_page, 'next_page': next_page}
