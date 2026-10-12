"""Real Codex + preview process: recover an HTTP 503 during native compaction."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from pytest import MonkeyPatch
from agent.harnesses.codex_harness import CodexAgent
from test_codex_sdk_transport import native_yield_case


def demonstrate(output, *, baseline_ref=None, outage_seconds=4):
    output.mkdir(parents=True, exist_ok=True)
    cases = [('AFTER', CodexAgent)]
    if baseline_ref:
        source = subprocess.check_output(['git', 'show',
            baseline_ref + ':agent/harnesses/codex_harness.py'], cwd=ROOT, text=True)
        baseline = ModuleType('agent.harnesses._codex_baseline')
        baseline.__file__ = str(ROOT / 'agent/harnesses/codex_harness.py')
        exec(compile(source, baseline.__file__, 'exec'), baseline.__dict__)
        cases.insert(0, ('BEFORE', baseline.CodexAgent))
    started = time.monotonic()
    rows, proofs = [], {}
    with (output / 'compaction-recovery.cast').open('w') as cast:
        cast.write(json.dumps({'version': 2, 'width': 120, 'height': 36, 'timestamp': int(time.time())}) + '\n')

        def say(message):
            elapsed = round(time.monotonic() - started, 3)
            print(message, flush=True)
            rows.append({'seconds': elapsed, 'message': message})
            cast.write(json.dumps([elapsed, 'o', message + '\r\n']) + '\n')
            cast.flush()

        say('Moyai compaction recovery: pinned real Codex 0.161.0 + BrokerRelay + SQLite')
        say('Model replies and broker outage are local fixtures; preview is a real process.')
        for label, cls in cases:
            say(label + ': one preview command; context 409; compaction 503; broker ready; continue.')
            with tempfile.TemporaryDirectory(prefix='moyai-compaction-') as directory, MonkeyPatch.context() as patch:
                proof = native_yield_case(Path(directory), patch, 1000, 'settle-context-preview',
                    agent_class=cls, compact_outage_seconds=outage_seconds, progress=say)
                proofs[label] = proof
                say(label + ': ' + proof['final_response'])
            if label == 'BEFORE':
                assert proof['failed'] and proof['pending_tools'] == 1
                assert proof['transport_attempt'] == 0 and proof['native_compactions'] == 1
                say('BEFORE reproduced: compaction failure exits with one unresolved tool.')
            else:
                assert proof['completed'] and not proof['pending_tools'] and not proof['faults']
                assert proof['native_clients'] == proof['native_threads'] == 1
                assert proof['command_starts'] == proof['tool_executions'] == proof['completed_receipts'] == 1
                assert proof['native_compactions'] == 2 and proof['transport_attempt'] == 1
                assert proof['original_request_receipts'] == 1 and proof['original_request_in_continuation']
                assert proof['readiness_statuses'][0] == 503 and proof['readiness_statuses'][-1] == 200
                assert {sample['phase'] for sample in proof['preview_samples']} == {'before', 'during', 'after'}
                assert len({(sample['pid'], sample['port']) for sample in proof['preview_samples']}) == 1
                assert all(sample['status'] == 200 for sample in proof['preview_samples'])
                say('PASS: same runtime/thread/preview PID; one command, one receipt, zero pending tools.')
                say('PASS: original request retained once; clean preview shutdown; task completed.')
        report = {'baseline_ref': baseline_ref, 'head': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'worktree_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True)),
            'harness_sha256': hashlib.sha256((ROOT / 'agent/harnesses/codex_harness.py').read_bytes()).hexdigest(),
            'inference': 'scripted local Responses replies; real pinned SDK, relay, command and journal',
            'proofs': proofs, 'timeline': rows}
        (output / 'compaction-recovery-proof.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-ref', help='Optional baseline from after the agent package move')
    parser.add_argument('--outage-seconds', type=float, default=4)
    args = parser.parse_args()
    if not 3 <= args.outage_seconds <= 40:
        parser.error('--outage-seconds must be between 3 and 40')
    demonstrate(args.output, baseline_ref=args.baseline_ref, outage_seconds=args.outage_seconds)
