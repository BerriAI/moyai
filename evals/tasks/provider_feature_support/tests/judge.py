from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

JUDGE_SYSTEM = """You grade one Slack reply written by a coding agent named Moyai to a "do we support this?" question about a provider docs page. You get the Slack thread, the agent's tool trace for this turn, the exact reply it posted, a reference reply written by a strong agent for the same situation, and case notes with the ground truth. The trace is the only evidence of what the agent did. Facts in the reply must be supported by the trace, the thread, or files the trace shows it read.

Score each criterion 0, 1, or 2:
- grounded: 2 if the verdict rests on checking the LiteLLM code in the trace and the reply says what it checked (a revision, file, or registry); 1 if it checked the code but does not say what it checked; 0 if it answered from memory or only from the docs page.
- correct: 2 if every factual claim matches the case notes and the trace; 1 for a minor slip; 0 for a material error such as claiming support that does not exist.
- complete: 2 if it gives a separate verdict for each API the linked page documents (Search and Fetch); 1 if it covers one clearly and the other vaguely; 0 if it answers only one or blurs them together.
- sized: 2 if it says concretely what adding support would take for what is missing (which file or pattern to follow, whether it is a new provider or a new endpoint); 1 if vague ("we could add it"); 0 if absent.
- offer: 2 if it ends by offering one specific next step the requester can answer yes to (for example opening a PR for the part that is easy) without having started that work unasked; 1 for a generic offer such as "let me know if you want help"; 0 if there is no offer, or it asked a clarifying question instead of answering.
- concise: 2 if it is Slack-sized with no apologies, self-narration, filler, or closing recap; 1 if somewhat padded; 0 if it reads like a document.

Also set unsupported_claim to true if the reply says or implies it ran, tested, or observed something the trace does not show. The reference shows one good answer, not the only one; do not penalize different wording or a different but valid structure. Return only JSON: {"grounded":n,"correct":n,"complete":n,"sized":n,"offer":n,"concise":n,"unsupported_claim":bool,"reason":"<=3 sentences"}."""
CRITERIA = ("grounded", "correct", "complete", "sized", "offer", "concise")
EM_DASH = "\u2014"
TLDR_LINE = re.compile(r"^[\W_]*tl;?\s?dr\b", re.IGNORECASE)
PR_TOOLS = ("github_create_pull_request",)


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def parse_judgment(raw: str) -> dict[str, Any]:
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Judge response did not contain a JSON object.")
    parsed = json.loads(candidate[start : end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("Judge response was not a JSON object.")
    return parsed


def first_line(reply: str) -> str:
    return next((line.strip() for line in reply.splitlines() if line.strip()), "")


def read_code(trace: list[dict[str, Any]]) -> bool:
    return any(
        "litellm/" in json.dumps(item.get("args", {})) or "SearchProviders" in item.get("result", "")
        for item in trace
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("case", "result", "requests", "output", "reward"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    case = read_json(args.case, {})
    result = read_json(args.result, {})
    trace = result.get("tool_trace", [])
    reply = result.get("posted_reply", "")
    tool_names = [item.get("name") for item in trace]
    gates = {
        "reply_posted": bool(reply.strip()),
        "tldr_first_line": bool(TLDR_LINE.match(first_line(reply))),
        "no_em_dash": EM_DASH not in reply,
        "no_question": "ask_user_question" not in tool_names,
        "read_code": read_code(trace),
        "no_unrequested_pr": not any(name in PR_TOOLS for name in tool_names),
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
                        "trace": trace,
                        "reply": reply,
                        "reference_reply": case.get("reference_reply", ""),
                        "case_notes": case.get("case_notes", ""),
                    }, ensure_ascii=False)},
                ],
                max_output_tokens=6000,
            )
            raw = response.output_text
            judgment = parse_judgment(raw)
    except Exception as error:  # noqa: BLE001 - judge failures must become trial failures
        judge_error = f"{type(error).__name__}: {error}"[:4000]
        if key := os.getenv("LITELLM_API_KEY") or os.getenv("LITELLM_GATEWAY_ADMIN_KEY"):
            judge_error = judge_error.replace(key, "[redacted]")
    failures = [f"gate:{name}" for name, passed in gates.items() if not passed]
    failures.extend(f"criterion:{name}" for name in case.get("required", []) if judgment.get(name) != 2)
    if judgment.get("unsupported_claim") is not False:
        failures.append("criterion:unsupported_claim")
    score = sum(
        value if isinstance(value, int) and not isinstance(value, bool) else 0
        for value in (judgment.get(name, 0) for name in CRITERIA)
    )
    if score < case.get("min_score", len(CRITERIA) * 2):
        failures.append("criterion:total_score")
    if judge_error:
        failures.append("judge:error")
    if not result:
        failures.append("execution:missing_result")
    report = {
        "gates": gates,
        "judgment": judgment,
        "score": score,
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
