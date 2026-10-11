"""Run native Codex search and a real Moyai MCP call with scripted local inference."""
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
from test_codex_tool_search import search_case
from test_workspace import workspace


def demonstrate(output):
    output.mkdir(parents=True, exist_ok=True)
    started, events = time.monotonic(), []

    def say(message):
        print(message, flush=True)
        events.append({'seconds': round(time.monotonic() - started, 3), 'message': message})

    say('Moyai native tool search · real Codex 0.161.0 + MCP + relay + broker')
    say('Scripted local Responses inference; no external account access or deployment.')
    with tempfile.TemporaryDirectory(prefix='moyai-search-proof-') as folder, MonkeyPatch.context() as patch:
        fixture = workspace.__wrapped__(Path(folder), patch)
        broker = next(fixture)
        try:
            proof, _, _ = search_case(Path(folder), patch, broker, progress=say)
        finally:
            fixture.close()
    (output / 'tool-search-results.json').write_text(json.dumps(proof, indent=2) + '\n')
    (output / 'tool-search-transcript.json').write_text(json.dumps(events, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    demonstrate(parser.parse_args().output)
