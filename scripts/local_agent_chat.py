"""Launch the local test chat with Harbor kept outside production dependencies."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(str(ROOT / path) for path in (
        "", "evals", "native/src", "native/tests",
    ))
    return subprocess.call([
        "uv", "run", "--frozen", "--project", str(ROOT / "native"), "--python", "3.13",
        "--with", "harbor==0.24.0", "--with", "openai==3.29.0", "--with", "litellm==1.107.0.dev3",
        "python", "-m", "moyai_harbor.chat", *sys.argv[1:],
    ], cwd=ROOT, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
