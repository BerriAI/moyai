"""Run the portable browser regression with the repository's Node dependencies."""
from pathlib import Path
import subprocess

subprocess.run(['node', '--test', 'tests/browser/personal_slack.cjs'],
               cwd=Path(__file__).resolve().parents[1], check=True)
