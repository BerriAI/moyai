"""Compare cold and reused Codex startup with real MCP and scripted inference."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
warnings.filterwarnings('ignore', message='Using .*starlette.testclient.*')

from pytest import MonkeyPatch
from agent.harnesses.codex_harness import CodexAgent
from sandbox.codex_runtime import RuntimeLease
from test_codex_tool_readiness import readiness_case


def demonstrate(output, pairs):
    output.mkdir(parents=True, exist_ok=True)
    samples, transcript = [], []
    started = time.monotonic()

    def say(message):
        print(message, flush=True)
        transcript.append({'seconds': round(time.monotonic() - started, 3), 'message': message})

    say('Moyai · Codex process reuse and catalog caching')
    say('Real pinned Codex + MCP; local scripted inference. No production traffic.')
    with tempfile.TemporaryDirectory(prefix='moyai-runtime-demo-', dir='/tmp') as folder:
        root = Path(folder)
        with MonkeyPatch.context() as patch:
            for index in range(pairs):
                for mode in ('cold', 'warm'):
                    sample = {'mode': mode, 'pair': index}

                    class MeasuredAgent(CodexAgent):
                        def __init__(self, **kwargs):
                            self.lease = None
                            if mode == 'warm':
                                self.lease = RuntimeLease('demo-session', 5, root=root / 'runtime')
                                kwargs['relay'].codex_runtime = self.lease
                            super().__init__(**kwargs)

                        def run_conversation(self, *args, **kwargs):
                            sample['started'] = time.monotonic()
                            return super().run_conversation(*args, **kwargs)

                        def before_model(self, request=None):
                            if 'startup_ms' not in sample:
                                sample['startup_ms'] = round((time.monotonic() - sample['started']) * 1000, 2)
                            return super().before_model(request)

                        def close(self):
                            sample['thread'] = self.runtime_thread
                            if self.lease:
                                sample.update(self.lease.info or {})
                                sample['clean'] = self.lease.clean
                                self.lease.close()
                            super().close()

                    proof, _, result = readiness_case(root, patch, delay=0, agent_class=MeasuredAgent)
                    assert proof['completed'] and proof['model_started_after_catalog'], result
                    sample.update(proof)
                    sample.pop('started')
                    samples.append(sample)
                    status = ('reused process ' if sample.get('reused') else 'started process ') + str(sample['pid']) if mode == 'warm' else 'fresh process'
                    say(f"{mode.upper()} {index + 1}: {sample['startup_ms']:.1f} ms before inference · {status} · MCP tool passed")
            warm = [row for row in samples if row['mode'] == 'warm']
            assert len({row['pid'] for row in warm}) == 1 and all(row['clean'] for row in warm)
            assert len({row['thread'] for row in samples}) == len(samples)
            # Omit the first pair so both paths have warm filesystem/Python caches;
            # the warm arm's first launch is reported separately, never hidden.
            steady = [row for row in samples if row['pair'] > 0]
            medians = {mode: statistics.median(row['startup_ms'] for row in steady if row['mode'] == mode)
                       for mode in ('cold', 'warm')}
            report = {'samples': samples, 'median_startup_ms': medians,
                      'first_warm_launch_ms': warm[0]['startup_ms'],
                      'same_process_reused': True, 'fresh_threads': True,
                      'catalog_ready_before_every_inference': True,
                      'measurement': 'run_conversation entry to first local inference request; excludes cloud provisioning and model generation'}
            say(f"MEDIAN after first pair: cold {medians['cold']:.1f} ms → reused {medians['warm']:.1f} ms")
            say('PASS: same process, fresh threads and permissions, real tool execution; finished threads unloaded.')
            (output / 'codex-runtime-results.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'codex-runtime-transcript.json').write_text(json.dumps(transcript, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=6)
    args = parser.parse_args()
    if args.pairs < 2:
        parser.error('--pairs must be at least 2')
    demonstrate(args.output, args.pairs)
