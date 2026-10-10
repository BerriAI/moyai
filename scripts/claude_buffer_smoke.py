"""Local before/after proof: uv run python scripts/claude_buffer_smoke.py.

Uses the pinned SDK, bundled Claude process and real native Read tool. Inference
is a deterministic loopback fixture; no provider credentials or requests.
"""
import json
from pathlib import Path
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))

from agent.harnesses.claude_harness import SDK_MAX_BUFFER_SIZE
from test_claude_message_buffer import large_image_case


def main():
    print('Claude message-buffer regression: real SDK + native image Read', flush=True)
    print('Inference: local deterministic fixture; no live provider calls.\n', flush=True)
    proofs = {}
    for label, sdk_default in [('BEFORE: SDK default, 1 MiB', True),
                               (f'AFTER: Moyai adapter, {SDK_MAX_BUFFER_SIZE // (1024 * 1024)} MiB', False)]:
        print(label, flush=True)
        with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
            proof = large_image_case(Path(directory), patch, sdk_default=sdk_default)
        proofs['before' if sdk_default else 'after'] = proof
        print('Completed:', proof['completed'], flush=True)
        print('Image received and visual marker verified:',
              proof['image_received'] and proof['image_marker_verified'], flush=True)
        print('Tool completion receipts:', proof['completed_tools'], '| pending:', proof['pending_tools'], flush=True)
        print('Largest accepted SDK frame:', json.dumps(proof['largest_frame']), flush=True)
        print('Result:', proof['answer'], '\n', flush=True)
    assert not proofs['before']['completed']
    assert proofs['before']['sdk_failure']['exception_type'] == 'CLIJSONDecodeError'
    after = proofs['after']
    assert after['completed'] and after['image_received'] and after['image_marker_verified']
    assert after['completed_tools'] == 1 and after['pending_tools'] == 0
    assert after['largest_frame']['bytes'] > 1024 * 1024
    print('PASS: same large-image workload fails at the default and completes with the adapter fix.', flush=True)
    print('The 16 MiB ceiling remains bounded. Model context limits are unchanged.', flush=True)
    return proofs


if __name__ == '__main__':
    main()
