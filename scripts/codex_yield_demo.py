"""Compare real Codex/MCP/relay behavior using synthetic local Responses."""
import argparse
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
from sandbox.codex_harness import CodexAgent
from test_codex_sdk_transport import native_yield_case


def demonstrate(baseline_ref, output):
    output.mkdir(parents=True, exist_ok=True)
    source = subprocess.check_output(['git', 'show', baseline_ref + ':sandbox/codex_harness.py'], cwd=ROOT)
    baseline = ModuleType('sandbox._yield_baseline')
    exec(compile(source, 'baseline_codex_harness.py', 'exec'), baseline.__dict__)
    started = time.monotonic()
    rows = []
    with (output / 'codex-yield.cast').open('w') as recording:
        recording.write(json.dumps({'version': 2, 'width': 110, 'height': 32, 'timestamp': int(time.time())}) + '\n')
        def say(message):
            elapsed = round(time.monotonic() - started, 3)
            print(message, flush=True)
            rows.append({'seconds': elapsed, 'message': message})
            recording.write(json.dumps([elapsed, 'o', message + '\r\n']) + '\n')
            recording.flush()
        say('Moyai: real Codex 0.161.0 + MCP + BrokerRelay + SQLite')
        say('Synthetic local model replies; no external provider or production changes')
        proofs = {}
        for label, cls in [('BEFORE', baseline.CodexAgent), ('AFTER', CodexAgent)]:
            say(label + ': run one tool, yield after 1 second, then poll it')
            with tempfile.TemporaryDirectory(prefix='moyai-yield-') as temporary, MonkeyPatch.context() as patch:
                proofs[label] = native_yield_case(Path(temporary), patch, 1000, agent_class=cls, progress=say)
        assert proofs['BEFORE']['failed'] and proofs['BEFORE']['boundary_failed']
        assert any(e.get('http_status') == 409 for e in proofs['BEFORE']['native_errors'])
        assert proofs['AFTER']['completed'] and not proofs['AFTER']['boundary_failed']
        assert proofs['AFTER']['tool_executions'] == proofs['AFTER']['completed_receipts'] == 1
        assert proofs['AFTER']['pending_tools'] == 0
        say('PASS: original adapter receives HTTP 409; fixed adapter completes')
        say('PASS: one tool execution, one saved receipt, zero unresolved tools')
        report = {'baseline_ref': baseline_ref, 'head': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'inference': 'synthetic local model replies', 'proofs': proofs, 'timeline': rows}
        (output / 'codex-yield-proof.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    demonstrate(args.baseline_ref, args.output)
