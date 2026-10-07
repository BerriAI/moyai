"""Reproduce and repair a pip-free runtime locally, without cloud credentials.

Run: uv run python scripts/workspace_image_demo.py --delay 4
"""
import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import venv

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delay', type=float, default=0)
    args = parser.parse_args()
    print('MOYAI WORKSPACE IMAGE — REAL LOCAL REPRODUCTION', flush=True)
    print('Uses a temporary Python environment. No production changes.\n', flush=True)
    with TemporaryDirectory(prefix='moyai-image-demo-') as directory:
        runtime = Path(directory) / 'runtime'
        venv.EnvBuilder(with_pip=False).create(runtime)
        python = str(runtime / 'bin' / 'python')
        print('1. Create the same pip-free environment shape used by Hermes.', flush=True)
        before = subprocess.run([python, '-m', 'pip', '--version'], capture_output=True, text=True)
        assert before.returncode and 'No module named pip' in before.stderr
        print('   Before: No module named pip — dependency installation cannot start.', flush=True)
        time.sleep(args.delay)
        print('\n2. Run the fixed installer bootstrap from this checkout.', flush=True)
        subprocess.run([python, '-c', 'from sandbox.harness_dependencies import ensure_pip; ensure_pip()'],
                       cwd=ROOT, check=True, capture_output=True, text=True, timeout=60)
        after = subprocess.run([python, '-m', 'pip', '--version'], check=True, capture_output=True, text=True)
        print('   After: ' + after.stdout.split(' from ')[0] + ' available in the runtime.', flush=True)
        time.sleep(args.delay)
        print('\n3. Run bootstrap again with subprocess installs prohibited.', flush=True)
        subprocess.run([python, '-c', "from unittest.mock import patch; "
                        "from sandbox.harness_dependencies import ensure_pip; "
                        "\nwith patch('subprocess.run', side_effect=AssertionError('unexpected install')): ensure_pip()"],
                       cwd=ROOT, check=True, capture_output=True, text=True, timeout=10)
        print('   PASS: Reuses the installed runtime; no second bootstrap.', flush=True)
        print('\nRESULT: Missing-pip failure reproduced, repaired, and rechecked.', flush=True)
        print('Full Linux image validation is a separate Modal build check.', flush=True)
        time.sleep(args.delay)


if __name__ == '__main__':
    main()
