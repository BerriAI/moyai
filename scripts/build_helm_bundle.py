"""Package a chart with the exact image published by the same release workflow."""
import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def build(source: Path, output: Path, digest: str, revision: str) -> None:
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest) or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Supply the published image digest and full source revision.')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        chart = Path(temporary) / 'moyai'
        shutil.copytree(source, chart)
        values = chart / 'values.yaml'
        text = values.read_text()
        if text.count('  digest: ""') != 1:
            raise ValueError('Expected one release-image digest placeholder.')
        values.write_text(text.replace('  digest: ""', f'  digest: "{digest}"'))
        subprocess.run(['helm', 'package', str(chart), '--app-version', revision,
                        '--destination', str(output.resolve())], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1] / 'charts/moyai', args.output, args.digest, args.revision)
