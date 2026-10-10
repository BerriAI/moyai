"""Scoped smart HTTP Git transport; the App credential never leaves Render."""
import base64
import re
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .connector_errors import ConnectorError

ZERO = '0' * 40
UPLOAD_LIMIT = 1024 * 1024
RECEIVE_LIMIT = 100 * 1024 * 1024
COMMAND_LIMIT = 64 * 1024


def receive_pack_commands(data):
    """Parse the leading pkt-line command section of a receive-pack request."""
    commands, offset = [], 0
    while True:
        if len(data) - offset < 4:
            raise ValueError('Truncated Git command section.')
        try:
            length = int(data[offset:offset + 4], 16)
        except ValueError:
            raise ValueError('Invalid Git command length.') from None
        offset += 4
        if length == 0:
            return commands
        if length < 4 or length > len(data) - offset:
            raise ValueError('Invalid Git command length.')
        commands.append(bytes(data[offset:offset + length - 4]))
        offset += length - 4
        if offset > COMMAND_LIMIT:
            raise ValueError('Git command section exceeds the limit.')


def routes(github, require_run):
    router = APIRouter()

    @router.api_route('/broker/{run_id}/github.git/{operation:path}', methods=['GET', 'POST'])
    @router.api_route('/broker/{run_id}/github/{owner}/{repository}.git/{operation:path}', methods=['GET', 'POST'])
    @router.api_route('/broker/{run_id}/github/repositories/{repository_id:int}.git/{operation:path}', methods=['GET', 'POST'])
    async def git(run_id: str, operation: str, request: Request, owner: str = '', repository: str = '', repository_id: int | None = None):
        def authorize():
            run = require_run(run_id, request)
            if 'github' not in run['plugins'] or not github.connectors.allowed('github_checkout'):
                raise HTTPException(403, 'GitHub is not enabled for this session.')
            return run
        run = authorize()
        advertisement = (request.method == 'GET' and operation == 'info/refs'
                         and list(request.query_params.multi_items()) in
                         [[('service', 'git-upload-pack')], [('service', 'git-receive-pack')]])
        receive = advertisement and request.query_params['service'] == 'git-receive-pack'
        pack = request.method == 'POST' and operation in {'git-upload-pack', 'git-receive-pack'} and not request.query_params
        if pack:
            receive = operation == 'git-receive-pack'
        if not (advertisement or pack):
            raise HTTPException(403, 'Only scoped Git fetch and Moyai publication pushes are available.')
        try:
            target = await github.selected_target(run, f'{owner}/{repository}' if owner else '', repository_id)
        except ConnectorError as exc:
            raise HTTPException(403, str(exc)) from None
        limit = RECEIVE_LIMIT if receive and pack else UPLOAD_LIMIT
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, 'Git negotiation exceeds the request limit.')
        if advertisement and data:
            raise HTTPException(400, 'Unexpected Git request body.')
        expected_type = 'application/x-git-receive-pack-request' if receive else 'application/x-git-upload-pack-request'
        if pack and request.headers.get('content-type') != expected_type:
            raise HTTPException(415, 'Expected a ' + ('receive' if receive else 'upload') + '-pack Git request.')
        encoding = request.headers.get('content-encoding', 'identity')
        if encoding not in {'identity', 'gzip'}:
            raise HTTPException(415, 'Unsupported Git encoding.')
        if receive and pack:
            try:
                commands = receive_pack_commands(data)
            except ValueError:
                raise HTTPException(400, 'Invalid Git receive-pack commands.') from None
            if not commands:
                raise HTTPException(403, 'A publication push must update a Moyai branch.')
            for command in commands:
                fields = command.split(b'\0', 1)[0].decode('utf-8', 'replace').split(' ')
                if len(fields) != 3:
                    raise HTTPException(403, 'Only branch updates may be pushed.')
                old, new, ref = fields
                branch = ref.removeprefix('refs/heads/')
                pending = (re.fullmatch(r'[0-9a-f]{40}', old) and re.fullmatch(r'[0-9a-f]{40}', new)
                           and new != ZERO and ref.startswith('refs/heads/')
                           and github.store.rows("SELECT 1 FROM github_publications WHERE run_id=? AND branch=? AND commit_sha=? AND result=''",
                                                 (run['id'], branch, new)))
                if not pending:
                    raise HTTPException(403, 'Only the pending Moyai publication commit may be pushed.')
        version = github.connection_version()
        token = await github.installation_token(repository=target, write=receive)
        authorize()
        if version != github.connection_version():
            raise HTTPException(409, 'The GitHub connection changed. Start checkout again.')
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        headers = {'Authorization': 'Basic ' + auth, 'Accept-Encoding': 'identity', 'User-Agent': 'Moyai'}
        if request.headers.get('git-protocol') == 'version=2':
            headers['Git-Protocol'] = 'version=2'
        if pack:
            headers.update({'Content-Type': expected_type, 'Content-Encoding': encoding})
        service = 'git-receive-pack' if receive else 'git-upload-pack'
        url = f'https://github.com/{github.repository_name(target)}.git/{operation}'
        if advertisement:
            url += '?service=' + service
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20), follow_redirects=False)
        try:
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=bytes(data)), stream=True)
            expected = 'application/x-' + service + '-' + ('advertisement' if advertisement else 'result')
            if upstream.status_code != 200 or upstream.headers.get('content-type', '').split(';')[0] != expected:
                await upstream.aclose()
                raise HTTPException(502, 'GitHub did not provide a Git response. Check repository access.')
        except BaseException as exc:
            await client.aclose()
            if isinstance(exc, httpx.HTTPError):
                raise HTTPException(502, 'GitHub checkout could not be reached.') from None
            raise

        async def stream():
            checked = 0
            try:
                async for chunk in upstream.aiter_bytes(65536):
                    if time.monotonic() - checked > 1:
                        authorize()
                        if version != github.connection_version():
                            return
                        checked = time.monotonic()
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
        return StreamingResponse(stream(), media_type=expected, headers={'Cache-Control': 'no-store'})

    return router
