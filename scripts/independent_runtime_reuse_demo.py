"""Actual controller, database, Codex and MCP; simulated Modal and inference."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
warnings.filterwarnings('ignore', message='Using .*starlette.testclient.*')

from pytest import MonkeyPatch
from agent.harnesses.codex_harness import CodexAgent
from app.config import MODEL_CATALOG, Settings
from app.db import Store
from app.temporal_runtime import TemporalRunManager
from sandbox.codex_runtime import RuntimeLease
from test_codex_tool_readiness import readiness_case
from test_durable import Cloud, drive


async def demonstrate(output, *, disable_model=False):
    output.mkdir(parents=True, exist_ok=True)
    print('Actual Moyai controller + SQLite + pinned Codex SDK + MCP.', flush=True)
    print('Modal allocation and model responses are local fixtures; no production traffic.', flush=True)
    proofs, runtime_roots = [], {}
    with tempfile.TemporaryDirectory(prefix='moyai-warm-proof-', dir='/tmp') as folder, MonkeyPatch.context() as patch:
        root = Path(folder)
        settings = Settings(_env_file=None, data_dir=root / 'data', session_secret='local-demo-only',
            agent_model='openai/gpt-6-astra', independent_agent_runtime=True, sandbox_idle_seconds=300)
        store = Store(settings.data_dir)
        run = store.create_run('Read the connected source.', '', 'modal', [], chat_enabled=True,
                               harness='codex', model=settings.agent_model)
        cloud = Cloud(patch, store, run['id'])
        manager = cloud.attach(TemporalRunManager(store, settings))
        manager.submit(run)
        original_command = cloud.command

        async def command(machine, action, directory, value, *, token=None):
            if action == 'start' and directory not in machine.operations:
                spec = dict(machine.spec)
                assert spec['independent_runtime'] and spec['codex_runtime_idle_seconds'] == 300
                runtime_root = root / machine.object_id
                runtime_roots[runtime_root] = spec['codex_runtime_scope']
                proof = {'sandbox': machine.object_id}

                class IndependentAgent(CodexAgent):
                    def __init__(self, **kwargs):
                        kwargs['spec']['independent_runtime'] = True
                        kwargs['config']['mcp_servers']['workspace']['env']['MOYAI_REMOTE_COMPUTER'] = '1'
                        self.lease = RuntimeLease(spec['codex_runtime_scope'], 300, root=runtime_root)
                        kwargs['relay'].codex_runtime = self.lease
                        super().__init__(**kwargs)

                    def close(self):
                        proof.update(self.lease.info or {})
                        proof['thread'] = self.runtime_thread
                        super().close()
                        proof['clean'] = self.lease.clean
                        self.lease.close()

                verified, _, result = await asyncio.to_thread(readiness_case, root, patch,
                    delay=0, agent_class=IndependentAgent, capability=token,
                    tool_name='first_lookup' if not proofs else 'fresh_lookup')
                assert verified['completed'] and verified['model_started_after_catalog'], result
                proof['tools'] = verified['tool_calls']
                proofs.append(proof)
            return await original_command(machine, action, directory, value, token=token)

        manager.command = command
        try:
            print('1. Sending the first app-only message…', flush=True)
            await drive(manager, run['id'], phase='warm')
            assert len(cloud.machines) == 1 and cloud.machines[0].alive
            assert cloud.snapshots == 0 and not store.run(run['id'])['sandbox_id']
            print(f'PASS: first turn finished; control guest {proofs[0]["sandbox"]} retained. Full computers: 0.', flush=True)
            print(f'      Codex process {proofs[0]["pid"]}; idle capability revoked.', flush=True)

            # A new controller reads the durable warm state after worker replacement.
            manager = cloud.attach(TemporalRunManager(Store(settings.data_dir), settings))
            manager.command = command
            print('2. Replacing the worker, then sending a follow-up…', flush=True)
            manager.store.enqueue_message(run['id'], 'Read again with current tools.', 'followup')
            await drive(manager, run['id'], phase='warm')
            assert len(cloud.machines) == 1 and len(cloud.launches) == 2
            assert proofs[0]['pid'] == proofs[1]['pid'] and proofs[1]['reused']
            assert proofs[0]['thread'] != proofs[1]['thread']
            assert all(p['clean'] for p in proofs)
            assert cloud.launch_tokens[0] != cloud.launch_tokens[1]
            assert proofs[1]['tools'] == ['fresh_lookup']
            assert 'Saved answer' in json.dumps(cloud.machines[0].spec['history_fallback'])
            print(f'PASS: same control guest and Codex process {proofs[1]["pid"]}; no second allocation.', flush=True)
            print('PASS: fresh thread, rotated access token, current tools and saved conversation.', flush=True)

            if disable_model:
                print('3. Removing the saved model while the chat is idle…', flush=True)
                patch.delitem(MODEL_CATALOG, settings.agent_model)
                settings.agent_model = 'openai/gpt-6.1-sol'
            else:
                print('3. Stopping the idle chat…', flush=True)
                await manager.cancel(run['id'])
            await drive(manager, run['id'])
            assert not cloud.machines[0].alive and manager.state(run['id'])['phase'] == 'idle'
            print('PASS: retained guest released; both answers remain saved exactly once.', flush=True)
            assert len([m for m in manager.store.messages(run['id']) if m['role'] == 'assistant']) == 2
            if disable_model:
                assert manager.store.run(run['id'])['status'] == 'idle'
                assert all(m['status'] == 'completed' for m in manager.store.messages(run['id']))
                assert await manager.advance(run['id']) is False
                print('PASS: chat stays idle, both turns completed; no failure or retry loop.', flush=True)
            report = {'scope': 'Local simulated Modal/model, actual controller/SQLite/Codex/MCP',
                'control_allocations': len(cloud.machines), 'turns': len(cloud.launches),
                'full_computers': 0, 'same_native_process': True, 'fresh_capability': True,
                'worker_restart': True, 'stop_released_guest': not disable_model,
                'disabled_model_released_guest': disable_model, 'proofs': proofs}
            (output / 'warm-runtime-proof.json').write_text(json.dumps(report, indent=2) + '\n')
            print('VERIFIED: two turns, one control guest, one native process, zero full computers.', flush=True)
        finally:
            # Simulated Modal termination cannot kill an actual local SDK process.
            # Close its owner without a clean-release receipt before deleting files.
            for runtime_root, scope in runtime_roots.items():
                if (runtime_root / 'lease.sock').exists():
                    lease = RuntimeLease(scope, .01, root=runtime_root)
                    lease.ready()
                    lease.close()
            manager.store.close()
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--disable-model', action='store_true', help='Release by removing the saved model instead of Stop.')
    args = parser.parse_args()
    asyncio.run(demonstrate(args.output, disable_model=args.disable_model))
