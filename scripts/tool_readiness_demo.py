"""Run real Codex/MCP before and after the readiness fix with local inference."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
warnings.filterwarnings('ignore', message='Using .*starlette.testclient.*')

from pytest import MonkeyPatch
from scripts.harness_baseline import load_codex_baseline
from agent.harnesses.codex_harness import CodexAgent
from test_codex_tool_readiness import readiness_case


def demonstrate(baseline_ref, output):
    output.mkdir(parents=True, exist_ok=True)
    baseline = load_codex_baseline(ROOT, baseline_ref)
    started, events, proof = time.monotonic(), [], {}
    with (output / 'tool-readiness.cast').open('w') as recording:
        recording.write(json.dumps({'version': 2, 'width': 108, 'height': 28, 'timestamp': int(time.time())}) + '\n')

        def say(message):
            elapsed = round(time.monotonic() - started, 3)
            print(message, flush=True)
            events.append({'seconds': elapsed, 'message': message})
            recording.write(json.dumps([elapsed, 'o', message + '\r\n']) + '\n')
            recording.flush()

        say('Moyai tool readiness · real Codex 0.161.0 + real stdio MCP')
        say('Local inference fixtures; no cloud deployment or external writes.')
        for label, cls in [('BEFORE', baseline.CodexAgent), ('AFTER', CodexAgent)]:
            say('\n' + label + ': delay tool discovery by two seconds')
            with tempfile.TemporaryDirectory(prefix='moyai-tool-proof-') as folder, MonkeyPatch.context() as patch:
                proof[label], _, _ = readiness_case(Path(folder), patch, progress=say, agent_class=cls)
        assert not proof['BEFORE']['model_started_after_catalog'] and not proof['BEFORE']['tool_calls']
        assert proof['AFTER'] == {'model_started_after_catalog': True, 'tool_calls': ['echo'], 'completed': True}
        say('\nPASS: the first model request now waits, and the real MCP call succeeds.')
        (output / 'tool-readiness-results.json').write_text(json.dumps(proof, indent=2) + '\n')
        (output / 'tool-readiness-transcript.json').write_text(json.dumps(events, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='9d2030516aff52aa8daa94d88d3b49c797f63bdf')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    demonstrate(args.baseline, args.output)
