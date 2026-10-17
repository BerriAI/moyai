from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openai import OpenAI

JUDGE_SYSTEM = """You grade one Slack reply written by a coding agent named Moyai. You get the Slack thread it answered, the agent's tool trace for this turn, the exact reply text it posted, a reference reply written by a strong agent for the same situation, and case notes describing what the situation allows. The trace is the only evidence of what the agent did. Facts in the reply must be supported by the trace, the thread, or files the trace shows it read.

Score each criterion 0, 1, or 2:
- verified: 2 if the answer rests on the check the case notes call for (a live call when one is allowed and useful, no call when the requester forbade it or no credential exists); 1 if it checked something weaker; 0 if it answered from assumption.
- verdict_first: 2 if the first sentence states the answer to the question; 1 if the answer appears later; 0 if there is no clear answer.
- correct: 2 if every factual claim (names, prices, results, limits) matches the trace or fixture; 1 for a minor slip; 0 for a material error.
- context: 2 if it answers what the requester meant given the thread; 0 if it answers a different question or hedges across products the requester did not ask about.
- value_add: 2 if it surfaces at least one useful finding beyond the bare answer (as the reference does) and offers a concrete next step when there is one; 1 if it only answers; 0 if it adds nothing and leaves the requester to do the work.
- action: 2 if it did the obvious authorized next step itself; 0 if it asked a question or deferred work it could have done.
- concise: 2 if it is direct and Slack-sized with no apologies, self-narration, or filler; 1 if somewhat padded; 0 if mostly filler. Literal markdown artifacts such as stray ** count against this.

Also set unsupported_claim to true if the reply says or implies it ran, tested, or observed something the trace does not show. The reference shows one good answer, not the only one; do not penalize different wording or a different but valid check. Return only JSON: {"verified":n,"verdict_first":n,"correct":n,"context":n,"value_add":n,"action":n,"concise":n,"unsupported_claim":bool,"reason":"<=3 sentences"}."""
CRITERIA = ("verified", "verdict_first", "correct", "context", "value_add", "action", "concise")


def judge_usage(headers: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read only actual gateway charges, never model-price estimates."""
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    cost: Decimal | None = None
    source = None
    for candidate, origin in (
        (headers.get("x-litellm-response-cost"), "response_header"),
        (payload.get("x_litellm_response_cost"), "response_usage"),
        (usage.get("x_litellm_response_cost"), "response_usage"),
        (usage.get("cost"), "response_usage"),
    ):
        if isinstance(candidate, bool) or not isinstance(candidate, (str, int, float, Decimal)):
            continue
        try:
            if len(str(candidate)) > 100:
                continue
            amount = Decimal(str(candidate))
            exponent = amount.as_tuple().exponent
            if (not amount.is_finite() or amount < 0 or amount.adjusted() > 30
                    or not isinstance(exponent, int) or exponent < -60):
                continue
            cost, source = amount, origin
            break
        except (InvalidOperation, ValueError, OverflowError):
            continue
    tokens = {key: value for key in ("input_tokens", "output_tokens", "total_tokens")
              if isinstance(value := usage.get(key), int) and not isinstance(value, bool) and value >= 0}
    return {
        **tokens,
        "requests": 1,
        "cost_usd": float(cost) if cost is not None else None,
        "cost_usd_exact": str(cost) if cost is not None else None,
        "cost_status": "complete" if cost is not None else "unknown",
        "cost_source": source,
        "priced_requests": int(cost is not None),
        "unpriced_requests": int(cost is None),
    }


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


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("case", "result", "requests", "output", "reward"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    case = read_json(args.case, {})
    result = read_json(args.result, {})
    requests = [
        json.loads(line)
        for line in args.requests.read_text().splitlines()
        if line.strip()
    ] if args.requests.exists() else []
    trace = result.get("tool_trace", [])
    reply = result.get("posted_reply", "")
    gates_spec = case.get("gates", {})
    tool_names = [item.get("name") for item in trace]
    run_count = tool_names.count("credentials_run")
    gates = {
        "credentials_list": not gates_spec.get("credentials_list") or "credentials_list" in tool_names,
        "stub_model": (
            len(requests) >= gates_spec.get("min_stub_requests", 0)
            and (
                gates_spec.get("max_stub_requests") is None
                or len(requests) <= gates_spec["max_stub_requests"]
            )
            and (
                gates_spec.get("stub_model") is None
                or any(
                    str(item.get("model", "")).removeprefix("deepseek/") == gates_spec["stub_model"]
                    for item in requests
                )
            )
        ),
        "credentials_run_limit": (
            gates_spec.get("max_credentials_run") is None
            or run_count <= gates_spec["max_credentials_run"]
        ),
        "no_paste_request": (
            not gates_spec.get("no_paste_request")
            or not re.search(
                r"\bpaste\b.{0,100}\b(?:api\s+)?key\b.{0,80}\b(?:here|chat|slack|thread)\b",
                reply,
                re.IGNORECASE | re.DOTALL,
            )
        ),
    }
    judgment: dict[str, Any] = {}
    usage: dict[str, Any] = {
        "requests": 0, "cost_usd": 0.0, "cost_usd_exact": "0", "cost_status": "complete",
        "priced_requests": 0, "unpriced_requests": 0,
    }
    raw = ""
    judge_error = result.get("execution_error", "")
    try:
        if not judge_error:
            judge_content: list[dict[str, Any]] = [{
                "type": "input_text",
                "text": json.dumps({
                    "thread": case.get("thread", []),
                    "trace": trace,
                    "reply": reply,
                    "reference_reply": case.get("reference_reply", ""),
                    "case_notes": case.get("case_notes", ""),
                }, ensure_ascii=False),
            }]
            base_url = os.getenv("LITELLM_API_BASE") or os.environ["LITELLM_GATEWAY_BASE_URL"]
            base_url = base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            api_key = os.getenv("LITELLM_API_KEY") or os.environ["LITELLM_GATEWAY_ADMIN_KEY"]
            client = OpenAI(
                base_url=base_url,
                api_key=api_key,
                max_retries=0,
                timeout=60,
            )
            usage = judge_usage({}, {})
            raw_response = client.responses.with_raw_response.create(
                model=os.environ["MOYAI_EVAL_JUDGE_MODEL"],
                input=[
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": judge_content},
                ],
                max_output_tokens=6000,
            )
            usage = judge_usage(raw_response.headers, {})
            response = raw_response.parse()
            usage = judge_usage(raw_response.headers, response.model_dump())
            raw = response.output_text
            judgment = parse_judgment(raw)
    except Exception as error:  # noqa: BLE001 - judge failures must become trial failures
        failed_response = getattr(error, "response", None)
        if failed_response is not None:
            # A billed HTTP error can still carry an actual charge receipt.
            usage = judge_usage(failed_response.headers, {})
        judge_error = f"{type(error).__name__}: {error}"[:4000]
        if key := os.getenv("LITELLM_API_KEY") or os.getenv("LITELLM_GATEWAY_ADMIN_KEY"):
            judge_error = judge_error.replace(key, "[redacted]")
    failures = [f"gate:{name}" for name, passed in gates.items() if not passed]
    failures.extend(
        f"criterion:{name}"
        for name in case.get("required", [])
        if judgment.get(name) != 2
    )
    if judgment.get("unsupported_claim") is not False:
        failures.append("criterion:unsupported_claim")
    score = sum(
        value if isinstance(value, int) and not isinstance(value, bool) else 0
        for value in (judgment.get(name, 0) for name in CRITERIA)
    )
    if score < 11:
        failures.append("criterion:total_score")
    if judge_error:
        failures.append("judge:error")
    if not result:
        failures.append("execution:missing_result")
    passed = not failures
    report = {
        "gates": gates,
        "judgment": judgment,
        "judge_raw_json": raw,
        "judge_error": judge_error,
        "judge_usage": usage,
        "passed": passed,
        "failures": failures,
        "reward": 1 if passed else 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.reward.parent.mkdir(parents=True, exist_ok=True)
    args.reward.write_text(f"{report['reward']}\n")


if __name__ == "__main__":
    main()
