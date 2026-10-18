"""Regenerates evals/tasks/desktop_* from evals/desktop/. Run after editing the shared files."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE.parent / "tasks"
LAUNCH = {
    "license": "nohup python3 /eval/apps.py license >/tmp/app.log 2>&1 &\n",
}
PROMPTS = {
    "license": "there's a License Manager window open on your computer's desktop. "
               "what activation code does it show? reply with the code",
}
TASK_TOML = """schema_version = "1.4"

[metadata]

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 1200.0

[environment]
build_timeout_sec = 900.0
"""


def main() -> None:
    for name, prompt in PROMPTS.items():
        task = TASKS / f"desktop_{name}"
        shutil.rmtree(task, ignore_errors=True)
        env = task / "environment"
        (env / "eval").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "task.toml").write_text(TASK_TOML)
        (task / "instruction.md").write_text(prompt + "\n")
        shutil.copy(HERE / "Dockerfile", env / "Dockerfile")
        for file in ("apps.py", "start_desktop.sh"):
            shutil.copy(HERE / file, env / "eval" / file)
        (env / "eval" / "launch_apps.sh").write_text("#!/bin/sh\n" + LAUNCH[name])
        (env / "eval" / "case_id.json").write_text(json.dumps({"case_id": f"desktop_{name}"}) + "\n")
        (env / "eval" / "credentials.json").write_text("{}\n")
        (env / "eval" / "thread.json").write_text(json.dumps(
            [{"role": "human", "display_name": "Ishaan", "text": prompt}], indent=2) + "\n")
        shutil.copy(HERE / "verify.py", task / "tests" / "verify.py")
        (task / "tests" / "test.sh").write_text(
            "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\n"
            f"python3 /tests/verify.py --task {name} --result /eval/moyai_result.json "
            "--output /logs/verifier/judgment.json --reward /logs/verifier/reward.txt\n")


if __name__ == "__main__":
    main()
