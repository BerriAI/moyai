"""Compare real Codex/MCP/relay behavior using synthetic local Responses."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from pytest import MonkeyPatch
from scripts.harness_baseline import load_codex_baseline
from agent.harnesses.codex_harness import CodexAgent
from test_codex_sdk_transport import native_yield_case


def demonstrate(baseline_ref, output, *, settlement=False, context=False, pause=0):
    output.mkdir(parents=True, exist_ok=True)
    baseline = load_codex_baseline(ROOT, baseline_ref)
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
        time.sleep(pause)
        proofs = {}
        for label, cls in [('BEFORE', baseline.CodexAgent), ('AFTER', CodexAgent)]:
            say(label + (': reject context while a command still needs 12 seconds to finish' if context else
                         ': start a preview server, then finish with its command still running'
                         if settlement else ': run one tool, yield after 1 second, then poll it'))
            with tempfile.TemporaryDirectory(prefix='moyai-yield-') as temporary, MonkeyPatch.context() as patch:
                proofs[label] = native_yield_case(Path(temporary), patch, 1000,
                    'settle-context' if context else 'settle-preview' if settlement else 'complete',
                    agent_class=cls, progress=say, context_delay=12)
                say(label + ' response: ' + proofs[label]['final_response'])
            time.sleep(pause)
        assert proofs['BEFORE']['failed']
        if context:
            assert proofs['BEFORE']['pending_tools'] == 1
            assert 'tool outcomes are pending' in proofs['BEFORE']['final_response']
            assert proofs['AFTER']['native_clients'] == 1
        elif settlement:
            assert proofs['BEFORE']['pending_tools'] == 1
            assert 'premature-answer' not in proofs['AFTER']['saved_prose']
        else:
            assert proofs['BEFORE']['boundary_failed']
            assert any(e.get('http_status') == 409 for e in proofs['BEFORE']['native_errors'])
        assert proofs['AFTER']['completed'] and not proofs['AFTER']['boundary_failed']
        assert proofs['AFTER']['tool_executions'] == proofs['AFTER']['completed_receipts'] == 1
        assert proofs['AFTER']['pending_tools'] == 0
        say('PASS: #233 still fails; native compaction keeps the command alive and finishes' if context else
            'PASS: original adapter rejects final; fixed adapter settles the command and completes'
            if settlement else 'PASS: original adapter receives HTTP 409; fixed adapter completes')
        say('PASS: one tool execution, one saved receipt, zero unresolved tools')
        report = {'baseline_ref': baseline_ref, 'head': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'inference': 'synthetic local model replies', 'proofs': proofs, 'timeline': rows}
        (output / 'codex-yield-proof.json').write_text(json.dumps(report, indent=2) + '\n')
        time.sleep(pause)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--settlement', action='store_true')
    parser.add_argument('--context', action='store_true', help='Prove context recovery preserves a command beyond the old grace period')
    parser.add_argument('--pause', type=float, default=0, help='Pause between stages when recording a demo')
    args = parser.parse_args()
    demonstrate(args.baseline_ref, args.output, settlement=args.settlement, context=args.context, pause=args.pause)
