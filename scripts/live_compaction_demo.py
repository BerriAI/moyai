"""Record real native SDK overlap against scripted local model responses."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def probe(root, harness, baseline):
    # Load application/SDK adapters from the selected checkout, with exactly the
    # same HTTP/tool fixture from this PR for both sides of the comparison.
    sys.path[:0] = [str(root), str(ROOT / 'tests')]
    import app.context_compaction as compaction
    if baseline:
        # A fixture discriminator only; baseline has no private summary path.
        compaction.PRIVATE_INSTRUCTIONS = 'Summarize the complete conversation prefix supplied below for the same ongoing task.'
    from pytest import MonkeyPatch
    from test_codex_sdk_transport import native_background_case
    from test_workspace import workspace
    with tempfile.TemporaryDirectory(prefix='moyai-live-context-') as directory, MonkeyPatch.context() as patch:
        fixture = workspace.__wrapped__(Path(directory), patch)
        try:
            proof = native_background_case(Path(directory), patch, next(fixture), harness,
                progress=lambda message: print(message, flush=True), expect_background=not baseline)
            proof['application_root'] = str(root)
            proof['revision'] = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
            print('PROOF ' + json.dumps(proof), flush=True)
        finally:
            fixture.close()


def record(baseline_root, output):
    output.mkdir(parents=True, exist_ok=True)
    started, rows, proofs = time.monotonic(), [], {}
    with (output / 'live-compaction.cast').open('w') as recording:
        recording.write(json.dumps({'version': 2, 'width': 120, 'height': 30, 'timestamp': int(time.time())}) + '\n')
        def say(message):
            elapsed = round(time.monotonic() - started, 3)
            print(message, flush=True)
            rows.append([elapsed, 'o', message + '\r\n'])
            recording.write(json.dumps(rows[-1]) + '\n')
            recording.flush()
        say('Moyai: background compaction with real SDKs, commands, HTTP relay and SQLite')
        say('Model replies and token counts are scripted locally; no production changes')
        for label, root, harness, before in [('BEFORE', baseline_root, 'codex', True),
                ('AFTER Codex', ROOT, 'codex', False), ('AFTER Claude', ROOT, 'claude-agent-sdk', False)]:
            say(label + ': continue working while an older history prefix is summarized')
            command = [sys.executable, str(Path(__file__).resolve()), '--probe-root', str(root), '--harness', harness]
            if before:
                command.append('--baseline')
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
                for line in child.stdout:
                    if line.startswith('PROOF '):
                        proofs[label] = json.loads(line.removeprefix('PROOF '))
                    else:
                        say(line.rstrip())
                if child.wait():
                    raise RuntimeError(label + ' proof failed; see the captured real output')
        say('PASS: foreground calls overlap the held summary; new history survives adoption')
        say('PASS: the original Codex command finishes in its original session, with no replay')
    (output / 'proof.json').write_text(json.dumps({'inference': 'scripted local replies and counting',
        'proofs': proofs, 'timeline': rows}, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--probe-root', type=Path)
    parser.add_argument('--harness', default='codex')
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    if args.probe_root:
        probe(args.probe_root, args.harness, args.baseline)
    else:
        if args.baseline_root is None or args.output is None:
            parser.error('--baseline-root and --output are required')
        record(args.baseline_root, args.output)
