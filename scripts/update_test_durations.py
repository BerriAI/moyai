"""Convert complete native CI JUnit reports into pytest-split timing hints."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from xml.etree import ElementTree


def durations_from_reports(reports: list[Path], root: Path) -> dict[str, float]:
    durations = {}
    for report in reports:
        cases = ElementTree.parse(report).findall('.//testcase')
        if not cases:
            raise ValueError(f'No test cases in {report}')
        for case in cases:
            if case.find('failure') is not None or case.find('error') is not None:
                raise ValueError('Use reports from a successful complete test run')
            parts = case.attrib['classname'].split('.')
            # Resolve the module separately from optional test class names.
            for end in range(len(parts), 0, -1):
                module = Path(*parts[:end]).with_suffix('.py')
                if (root / module).is_file():
                    break
            else:
                raise ValueError(f'Test module no longer exists: {case.attrib["classname"]}')
            node = '::'.join([module.as_posix(), *parts[end:], case.attrib['name']])
            seconds = float(case.attrib['time'])
            if not math.isfinite(seconds) or seconds < 0 or node in durations:
                raise ValueError(f'Invalid or duplicate timing: {node}')
            durations[node] = seconds
    return dict(sorted(durations.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--root', type=Path, default=Path('native'))
    parser.add_argument('--output', type=Path, default=Path('native/.test_durations'))
    args = parser.parse_args()
    durations = durations_from_reports(args.reports, args.root)
    args.output.write_text(json.dumps(durations, indent=2) + '\n')
    print(f'Wrote {len(durations)} test timings to {args.output}')


if __name__ == '__main__':
    main()
