"""AWS Lambda sandbox conformance: --local-image IMAGE or --live (uses AWS settings)."""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.sandboxes.lambda_microvm import LambdaProvider
from scripts.substrate_smoke import verify_agent_image, verify_computer, verify_environment_build


async def execute(sandbox, code, timeout=60, python='/usr/local/bin/python'):
    process = await sandbox.exec.aio(python, '-c', code, timeout=timeout)
    out, err = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
    assert await process.wait.aio() == 0, out + '\n' + err
    return out


async def verify(backend):
    machines, snapshots = [], []
    try:
        original = await backend.create(name='moyai-smoke-' + uuid4().hex, token='parent-only-capability',
                                         timeout=1800, apt_packages=('tree',))
        machines.append(original)
        await execute(original, 'from pathlib import Path; Path("/workspace").mkdir(exist_ok=True); '
            'Path("/session").mkdir(exist_ok=True); Path("/opt/test-dependency").mkdir(exist_ok=True)')
        await original.filesystem.write_text.aio('untracked ✓', '/workspace/ignored/node_modules/package/index.js')
        await original.filesystem.write_text.aio('conversation ✓', '/session/conversation.json')
        await original.filesystem.write_text.aio('installed dependency ✓', '/opt/test-dependency/package.py')
        await execute(original, 'import os; from pathlib import Path; '
            'Path("/usr/local/bin/moyai-test-tool").write_text("#!/bin/sh\\necho executable\\n"); '
            'os.chmod("/usr/local/bin/moyai-test-tool", 0o755); '
            'Path("/workspace/link").symlink_to("ignored/node_modules/package/index.js"); '
            'Path("/opt/hermes/README.md").unlink()')
        assert 'stdout ✓' in await execute(original, 'import sys; print("stdout ✓"); print("stderr",file=sys.stderr)')
        timed = await original.exec.aio('sleep', '30', timeout=1)
        assert await timed.wait.aio() == 124
        await execute(original, 'from run_agent import AIAgent; import mcp, claude_agent_sdk; print("full agent image ready")', python='/opt/hermes-env/bin/python')
        print('PASS full agent image, real package installation, commands, streams, timeout, files', flush=True)
        await verify_agent_image(original)
        await verify_environment_build(original)
        await verify_computer(original)
        assert (await original.computer_request({'action': 'state'}))['available']
        writer = await original.exec.aio('env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin',
            '/usr/local/bin/python', '-c',
            'import os,sys,time; from pathlib import Path\n'
            'if os.fork(): sys.exit(0)\nos.setsid()\nif os.fork(): sys.exit(0)\n'
            'p=Path("/workspace/counter"); temp=p.with_suffix(".next")\nwhile True:\n'
            ' temp.write_text(str(time.time_ns())); temp.replace(p); time.sleep(.1)', timeout=60)
        assert await writer.wait.aio() == 0
        for _ in range(100):
            try:
                await original.filesystem.stat.aio('/workspace/counter')
                break
            except FileNotFoundError:
                await asyncio.sleep(.05)
        else:
            raise AssertionError('Detached background writer did not start')
        # An abandoned durable execution is never restarted in the clone.
        await original.filesystem.write_text.aio('{"pid":12345}', '/session/executions/uncertain/started.json')
        snapshot = await original.snapshot_filesystem.aio(timeout=600)
        snapshots.append(snapshot.object_id)
        before = await original.filesystem.read_bytes.aio('/workspace/counter')
        await asyncio.sleep(.3)
        assert before != await original.filesystem.read_bytes.aio('/workspace/counter')
        print('PASS checkpoint while background command runs; original command resumes after save', flush=True)
        await original.terminate.aio()
        assert await original.poll.aio() is not None
        restored = await backend.create(name='moyai-smoke-' + uuid4().hex, snapshot_id=snapshot.object_id,
                                         token='child-only-capability', timeout=1800)
        machines.append(restored)
        assert restored.object_id != original.object_id
        assert (await restored.filesystem.read_bytes.aio('/workspace/ignored/node_modules/package/index.js')).decode() == 'untracked ✓'
        assert (await restored.filesystem.read_bytes.aio('/session/conversation.json')).decode() == 'conversation ✓'
        assert (await restored.filesystem.read_bytes.aio('/opt/test-dependency/package.py')).decode() == 'installed dependency ✓'
        result = await execute(restored, 'import os,subprocess; from pathlib import Path; '
            'assert not Path("/opt/hermes/README.md").exists(); '
            'assert Path("/workspace/link").is_symlink(); '
            'assert subprocess.check_output(["/usr/local/bin/moyai-test-tool"]).strip()==b"executable"; '
            'assert subprocess.run(["tree","--version"],capture_output=True).returncode==0; '
            'assert os.environ["WORKSPACE_RUN_TOKEN"]=="child-only-capability"; '
            'import sys; sys.path.insert(0,"/opt/workspace-runner"); from durable_process import status; '
            'assert status(Path("/session/executions/uncertain"))["state"]=="uncertain"; print("restored")')
        assert result.strip() == 'restored'
        assert await restored.filesystem.read_bytes.aio('/usr/local/moyai-environment-proof') == b'prepared'
        await verify_computer(restored)
        assert (await restored.computer_request({'action': 'state'}))['available']
        print('PASS restored prepared environment and fresh Computer service through the VM endpoint', flush=True)
        counter = await restored.filesystem.read_bytes.aio('/workspace/counter')
        await asyncio.sleep(.3)
        assert counter == await restored.filesystem.read_bytes.aio('/workspace/counter')
        await restored.filesystem.write_text.aio('child mutation', '/session/conversation.json')
        await restored.terminate.aio()
        sibling = await backend.create(name='moyai-smoke-' + uuid4().hex, snapshot_id=snapshot.object_id, timeout=600)
        machines.append(sibling)
        assert (await sibling.filesystem.read_bytes.aio('/session/conversation.json')).decode() == 'conversation ✓'
        print('PASS source deleted, independent restores, dependencies, modes, symlinks, deletions, conversation, no process replay', flush=True)
        await sibling.terminate.aio()
        assert await sibling.poll.aio() is not None
        print('PASS confirmed termination and isolated sibling workspace', flush=True)
    finally:
        for sandbox in machines:
            await sandbox.terminate.aio()
        for snapshot in snapshots:
            manifest_key = snapshot.split(':', 3)[3]
            for key in (manifest_key, manifest_key[:-5] + '.tar.gz'):
                await backend.aws('s3', 'delete_object', Bucket=backend.settings.lambda_checkpoint_bucket, Key=key)


async def main(args):
    if args.live:
        settings = Settings(sandbox_provider='lambda')
        if settings.missing_sandbox():
            raise SystemExit('Missing settings: ' + ', '.join(settings.missing_sandbox()))
        await verify(LambdaProvider(settings))
        return
    source = Path(__file__).resolve().parents[1] / 'tests/fixtures/lambda/local_aws.py'
    spec = importlib.util.spec_from_file_location('local_lambda_aws', source)
    local = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(local)
    fixture = local.LocalAWS(Settings(_env_file=None, lambda_image='local-image', lambda_image_version='test',
        lambda_checkpoint_bucket='local-checkpoints', lambda_checkpoint_prefix='smoke'), args.local_image)
    client = httpx.AsyncClient
    try:
        with patch('httpx.AsyncClient', lambda **kw: client(transport=httpx.MockTransport(fixture.map_request), **kw)):
            await verify(fixture.backend)
    finally:
        await fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--local-image')
    asyncio.run(main(parser.parse_args()))
