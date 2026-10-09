"""Live Substrate conformance test; requires a disposable, configured cluster.

Never substitutes a fake control plane. All actors and tags created here are
deleted in finally blocks. No model or third-party account credentials needed.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.sandboxes.substrate import SubstrateProvider


async def execute(sandbox, code, timeout=30):
    process = await sandbox.exec.aio('/usr/local/bin/python', '-c', code, timeout=timeout)
    out, err = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
    assert await process.wait.aio() == 0, err
    return out


async def verify_agent_image(sandbox):
    """Run the real SDK and MCP bridge with deterministic, local inference."""
    tests = Path(__file__).resolve().parents[1] / 'tests/test_claude_sdk_transport.py'
    await sandbox.filesystem.write_text.aio(tests.read_text(), '/opt/validation/tests/test_claude_sdk_transport.py')
    process = await sandbox.exec.aio('sh', '-ec',
        'ln -s /opt/workspace-runner /opt/validation/sandbox; '
        '/opt/hermes-env/bin/python -m pip install pytest; '
        # First use of the large SDK executable can fault cold AWS image pages.
        'MOYAI_SDK_TEST_TIMEOUT=120 PYTHONPATH=/opt/validation:/opt/hermes /opt/hermes-env/bin/python -m pytest -q '
        '/opt/validation/tests/test_claude_sdk_transport.py -k "new-session or durable-checkpoint"', timeout=240)
    out, err = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
    assert await process.wait.aio() == 0, out + '\n' + err
    assert '6 passed' in out, out
    print('PASS real agent SDK, MCP tools, and durable conversation continuation (local inference fixture)', flush=True)


async def verify_environment_build(sandbox):
    recipe = {'repository': 'octocat/Hello-World', 'ref': 'master', 'setup_mode': 'manual',
              'setup': 'printf prepared > /usr/local/moyai-environment-proof',
              'startup': '', 'verify': 'test -f README', 'shutdown': ''}
    await sandbox.filesystem.write_text.aio(json.dumps(recipe), '/tmp/moyai-environment.json')
    await execute(sandbox, 'import sys; sys.path.insert(0,"/opt/workspace-runner"); import environment_build; environment_build.main("start")')
    async with asyncio.timeout(240):
        while True:
            result = json.loads(await execute(sandbox,
                'import sys,json; sys.path.insert(0,"/opt/workspace-runner"); import environment_build; print(json.dumps(environment_build.main("status")))'))
            if result.get('done'):
                assert result.get('success'), result
                break
            await asyncio.sleep(1)
    assert await sandbox.filesystem.read_bytes.aio('/usr/local/moyai-environment-proof') == b'prepared'
    print('PASS outbound GitHub access and real environment build supervisor', flush=True)


async def verify_computer(sandbox):
    result = await execute(sandbox, r'''
import asyncio, sys, threading, json, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, '/opt/workspace-runner')
import computer
class Page(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        page = b'<title>Moyai</title><h1>Ready</h1><button onclick="document.querySelector(\'h1\').textContent=\'Clicked\'">Test</button>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(page)
server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    opened = computer.request({'action':'open','args':{'url':f'http://127.0.0.1:{server.server_port}'}})
    if 'error' in opened:
        # The public service deliberately hides browser internals. Diagnose
        # this synthetic local page without weakening the original assertion.
        async def diagnose():
            probe = computer.Computer()
            try:
                await probe.command({'action':'open','args':{'url':f'http://127.0.0.1:{server.server_port}'}})
                print('Direct Computer probe succeeded after service failure.', file=sys.stderr)
            except Exception:
                traceback.print_exc()
            finally:
                if probe.browser:
                    await probe.browser.close()
                if probe.playwright:
                    await probe.playwright.stop()
        asyncio.run(diagnose())
    assert opened.get('title') == 'Moyai', opened
    clicked = computer.request({'action':'click','args':{'role':'button','name':'Test'}})
    assert 'Clicked' in clicked.get('text',''), clicked
    captured = computer.request({'action':'screenshot','args':{'name':'substrate-computer'}})
    assert 'path' in captured, captured
    print(json.dumps(captured))
finally:
    server.shutdown()
    server.server_close()
''', timeout=300)
    assert (await sandbox.filesystem.read_bytes.aio(json.loads(result)['path'])).startswith(b'\x89PNG')
    print('PASS Moyai Computer service, visible Chromium/Xvfb, click and saved screenshot', flush=True)


async def main():
    settings = Settings()
    backend = SubstrateProvider(settings)
    actors, tags = [], []
    try:
        original = await backend.create(name='moyai-smoke-' + uuid4().hex, token='parent-test-capability', timeout=900)
        actors.append(original)
        print('PASS create, authentication, resume', flush=True)
        original_boot = (await original.request('/activate', {}))['boot_id']
        await original.filesystem.write_text.aio('persisted ✓', '/workspace/proof.txt')
        assert (await original.filesystem.read_bytes.aio('/workspace/proof.txt')).decode() == 'persisted ✓'
        assert 'out ✓' in await execute(original, 'import sys; print("out ✓"); print("err", file=sys.stderr)')
        timed = await original.exec.aio('/usr/local/bin/python', '-c', 'import time; time.sleep(30)', timeout=1)
        assert await timed.wait.aio() == 124
        await execute(original, 'from pathlib import Path; Path("/usr/local/rootfs-proof").write_text("rootfs")')
        print('PASS command, stdout/stderr, file read/write', flush=True)
        await original.exec.aio('/usr/local/bin/python', '-c',
            'import time; from pathlib import Path\np=Path("/workspace/counter"); tmp=p.with_suffix(".next")\nwhile True:\n tmp.write_text(str(int(p.read_text() if p.exists() else "0")+1)); tmp.replace(p); time.sleep(.1)', timeout=300)
        await asyncio.sleep(1)
        snapshot = await original.snapshot_filesystem.aio(timeout=300)
        tags.append(snapshot.object_id)
        parent_before = await execute(original, 'from pathlib import Path; print(Path("/workspace/counter").read_text())')
        clone = await backend.create(name='moyai-clone-' + uuid4().hex, snapshot_id=snapshot.object_id, token='child-test-capability', timeout=900)
        actors.append(clone)
        assert (await clone.request('/activate', {}))['boot_id'] != original_boot, 'Clone retained parent server memory!'
        assert (await original.request('/activate', {}))['boot_id'] == original_boot
        assert await clone.filesystem.read_bytes.aio('/usr/local/rootfs-proof') == b'rootfs'
        value = await clone.filesystem.read_bytes.aio('/workspace/counter')
        await asyncio.sleep(1)
        assert await clone.filesystem.read_bytes.aio('/workspace/counter') == value, 'Clone resumed parent process!'
        assert int(await execute(original, 'from pathlib import Path; print(Path("/workspace/counter").read_text())')) > int(parent_before)
        assert (await execute(clone, 'import os; print(os.environ["WORKSPACE_RUN_TOKEN"])')).strip() == 'child-test-capability'
        print('PASS filesystem checkpoint, clone isolation, original process continuation', flush=True)
        reconnected = await SubstrateProvider(settings).get(clone.object_id)
        assert await reconnected.filesystem.read_bytes.aio('/workspace/proof.txt') == 'persisted ✓'.encode()
        print('PASS reconnect from persisted sandbox ID', flush=True)
        output = await execute(clone, 'from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n b=p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"]); page=b.new_page(); page.set_content("<h1>Moyai</h1>"); print(page.locator("h1").inner_text()); page.screenshot(path="/workspace/browser.png"); b.close()')
        assert output.strip() == 'Moyai'
        assert (await clone.filesystem.read_bytes.aio('/workspace/browser.png')).startswith(b'\x89PNG')
        print('PASS real Chromium and screenshot artifact', flush=True)
        await verify_computer(clone)
        if os.environ.get('MOYAI_SMOKE_FULL_IMAGE'):
            await verify_agent_image(clone)
            await verify_environment_build(clone)
            await original.terminate.aio()
            prepared = await clone.snapshot_filesystem.aio(timeout=300)
            tags.append(prepared.object_id)
            from_environment = await backend.create(snapshot_id=prepared.object_id, timeout=120)
            actors.append(from_environment)
            assert await from_environment.filesystem.read_bytes.aio('/usr/local/moyai-environment-proof') == b'prepared'
            print('PASS new sandbox from prepared environment snapshot', flush=True)
        await clone.terminate.aio()
        from modal.exception import NotFoundError
        try:
            await backend.get(clone.object_id)
        except NotFoundError:
            pass
        else:
            raise AssertionError('Deleted actor still exists')
        print('PASS cancellation and cleanup', flush=True)
    finally:
        for sandbox in actors:
            await sandbox.terminate.aio()
        for identity in tags:
            _, space, _, tag = identity.split(':')
            await backend.rpc('DeleteTag', {'tag': {'atespace': space, 'name': tag}})


if __name__ == '__main__':
    asyncio.run(main())
