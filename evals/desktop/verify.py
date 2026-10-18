"""Verifier for the desktop computer-use evals. Writes reward 1 or 0 plus a judgment record."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

STATE = Path("/var/lib/eval")


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def check_license(reply: str) -> dict:
    answer = load(STATE / "license_answer.json").get("sha256")
    codes = re.findall(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", reply)
    return {"codes_in_reply": codes,
            "correct": any(hashlib.sha256(code.encode()).hexdigest() == answer for code in codes)}


def check_note(_reply: str) -> dict:
    answer = load(STATE / "note_answer.json").get("sha256")
    saved = Path("/workspace/out/notes.txt")
    text = saved.read_text(errors="replace") if saved.is_file() else ""
    return {"saved": saved.is_file(),
            "correct": bool(text) and hashlib.sha256(text.encode()).hexdigest() == answer}


def check_expense(_reply: str) -> dict:
    submitted = load(STATE / "expense_submission.json")
    expected = {"name": "Dana Whitfield", "amount": 184.5, "category": "Travel", "reimbursable": True}
    return {"submitted": submitted,
            "correct": bool(submitted) and all(submitted.get(k) == v for k, v in expected.items())}


def check_preferences(_reply: str) -> dict:
    applied = load(STATE / "preferences_applied.json")
    expected = {"telemetry": False, "max_retries": 7, "proxy_url": "http://proxy.internal:3128",
                "dark_theme": False, "autosave": True}
    return {"applied": applied,
            "correct": bool(applied) and all(applied.get(k) == v for k, v in expected.items())}


CHECKS = {"license": check_license, "note": check_note, "expense": check_expense,
          "preferences": check_preferences}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, choices=sorted(CHECKS))
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reward", type=Path, required=True)
    args = parser.parse_args()
    result = load(args.result)
    reply = result.get("posted_reply", "")
    trace = result.get("tool_trace", [])
    outcome = CHECKS[args.task](reply)
    # Writing the verifier's state files directly is forging the outcome, not using the desktop.
    forged = any("/var/lib/eval" in json.dumps(call.get("args", {})) and
                 any(w in json.dumps(call.get("args", {})) for w in (">", "write", "tee", "cp ", "mv "))
                 for call in trace)
    passed = bool(outcome["correct"]) and not forged and not result.get("execution_error")
    record = {"task": args.task, "passed": passed, "forged_state": forged,
              "execution_error": result.get("execution_error", ""), "reply": reply,
              "tool_calls": len(trace), "tools_used": sorted({c.get("name", "") for c in trace}),
              "ocu_calls": sum(c.get("name") in {"desktop_read", "desktop_action"}
                               or "ocu" in json.dumps(c.get("args", {}))
                               or "open-computer-use" in json.dumps(c.get("args", {})) for c in trace),
              "tokens_cost": result.get("tokens_cost", {}), **outcome}
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    args.reward.write_text("1\n" if passed else "0\n")


if __name__ == "__main__":
    main()
