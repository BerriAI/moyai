from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

JUDGE_SYSTEM = """You grade one Slack reply written by a coding agent named Moyai against its answer style rules. You get the Slack thread it answered, the exact reply text it posted, and a reference reply showing one good answer.

Score each criterion 0, 1, or 2:
- verdict_first: 2 if the first sentence directly answers the question; 1 if the answer appears later; 0 if there is no clear answer.
- direct: 2 if there is no filler opener (such as "Great question" or "I'll help you with"), no narration of its own steps, and no closing recap that repeats the answer; 1 for one minor instance; 0 for several.
- brief: 2 if it is Slack-sized and every sentence helps the reader decide or act; 1 if somewhat padded; 0 if it reads like a document.

Do not penalize different wording or a different but valid answer. Return only JSON: {"verdict_first":n,"direct":n,"brief":n,"reason":"<=3 sentences"}."""
CRITERIA = ("verdict_first", "direct", "brief")
EM_DASH = "\u2014"
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def parse_judgment(raw: str) -> dict[str, Any]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Judge response did not contain a JSON object.")
    parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("Judge response was not a JSON object.")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("case", "result", "requests", "output", "reward"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    case = read_json(args.case, {})
    result = read_json(args.result, {})
    reply = result.get("posted_reply", "")
    gates = {
        "reply_posted": bool(reply.strip()),
        "no_em_dash": EM_DASH not in reply,
        "no_emoji": not EMOJI.search(reply),
    }
    judgment: dict[str, Any] = {}
    raw = ""
    judge_error = result.get("execution_error", "")
    try:
        if not judge_error and reply.strip():
            base_url = (os.getenv("LITELLM_API_BASE") or os.environ["LITELLM_GATEWAY_BASE_URL"]).rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            client = OpenAI(
                base_url=base_url,
                api_key=os.getenv("LITELLM_API_KEY") or os.environ["LITELLM_GATEWAY_ADMIN_KEY"],
                max_retries=0,
                timeout=60,
            )
            response = client.responses.create(
                model=os.environ["MOYAI_EVAL_JUDGE_MODEL"],
                input=[
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": json.dumps({
                        "thread": case.get("thread", []),
                        "reply": reply,
                        "reference_reply": case.get("reference_reply", ""),
                    }, ensure_ascii=False)},
                ],
                max_output_tokens=2000,
            )
            raw = response.output_text
            judgment = parse_judgment(raw)
    except Exception as error:  # noqa: BLE001 - judge failures must become trial failures
        judge_error = f"{type(error).__name__}: {error}"[:4000]
        if key := os.getenv("LITELLM_API_KEY") or os.getenv("LITELLM_GATEWAY_ADMIN_KEY"):
            judge_error = judge_error.replace(key, "[redacted]")
    failures = [f"gate:{name}" for name, passed in gates.items() if not passed]
    failures.extend(f"criterion:{name}" for name in ("verdict_first", "direct") if judgment.get(name) != 2)
    if judgment.get("brief", 0) < 1:
        failures.append("criterion:brief")
    if judge_error:
        failures.append("judge:error")
    report = {
        "gates": gates,
        "em_dash_count": reply.count(EM_DASH),
        "judgment": judgment,
        "judge_raw_json": raw,
        "judge_error": judge_error,
        "passed": not failures,
        "failures": failures,
        "reward": 0 if failures else 1,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.reward.parent.mkdir(parents=True, exist_ok=True)
    args.reward.write_text(f"{report['reward']}\n")


if __name__ == "__main__":
    main()
