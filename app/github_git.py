"""Read-only smart HTTP Git transport; the App credential never leaves Render."""
import base64
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .connector_errors import ConnectorError


def routes(github, require_run):
    router = APIRouter()

    @router.api_route('/broker/{run_id}/github.git/{operation:path}', methods=['GET', 'POST'])
    @router.api_route('/broker/{run_id}/github/{owner}/{repository}.git/{operation:path}', methods=['GET', 'POST'])
    async def git(run_id: str, operation: str, request: Request, owner: str = '', repository: str = ''):
        def authorize():
            run = require_run(run_id, request)
            if 'github' not in run['plugins'] or not github.connectors.allowed('github_checkout'):
                raise HTTPException(403, 'GitHub is not enabled for this session.')
            return run
        run = authorize()
        advertisement = (request.method == 'GET' and operation == 'info/refs'
                         and list(request.query_params.multi_items()) == [('service', 'git-upload-pack')])
        pack = request.method == 'POST' and operation == 'git-upload-pack' and not request.query_params
        if not (advertisement or pack):
            raise HTTPException(403, 'Only read-only Git fetch is available. Use the approved PR tool to publish changes.')
        try:
            target = await github.selected_target(run, f'{owner}/{repository}' if owner else '')
        except ConnectorError as exc:
            raise HTTPException(403, str(exc)) from None
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise HTTPException(413, 'Git negotiation exceeds the request limit.')
        if advertisement and data:
            raise HTTPException(400, 'Unexpected Git request body.')
        if pack and request.headers.get('content-type') != 'application/x-git-upload-pack-request':
            raise HTTPException(415, 'Expected a Git upload-pack request.')
        encoding = request.headers.get('content-encoding', 'identity')
        if encoding not in {'identity', 'gzip'}:
            raise HTTPException(415, 'Unsupported Git encoding.')
        version = github.connection_version()
        token = await github.installation_token(repository=target)
        authorize()
        if version != github.connection_version():
            raise HTTPException(409, 'The GitHub connection changed. Start checkout again.')
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        headers = {'Authorization': 'Basic ' + auth, 'Accept-Encoding': 'identity', 'User-Agent': 'Moyai-Devin'}
        if request.headers.get('git-protocol') == 'version=2':
            headers['Git-Protocol'] = 'version=2'
        if pack:
            headers.update({'Content-Type': 'application/x-git-upload-pack-request', 'Content-Encoding': encoding})
        url = f'https://github.com/{target}.git/{operation}'
        if advertisement:
            url += '?service=git-upload-pack'
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20), follow_redirects=False)
        try:
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=bytes(data)), stream=True)
            expected = 'application/x-git-upload-pack-' + ('advertisement' if advertisement else 'result')
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
