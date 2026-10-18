from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

TASK = Path(__file__).resolve().parents[1] / "evals/tasks/answer_style"
spec = importlib.util.spec_from_file_location("answer_style_v2_judge", TASK / "tests/judge.py")
assert spec and spec.loader
judge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge)
CASE = json.loads((TASK / "tests/case.json").read_text())
CALIBRATION = json.loads((TASK / "tests/calibration.json").read_text())
PERFECT = {**dict.fromkeys(judge.CRITERIA, 2), "reason": "Concrete, correct and concise."}


def test_versioned_case_facts_are_sent_to_the_judge():
    messages = judge.judge_messages(CASE, "An alternate answer.")
    payload = json.loads(messages[1]["content"])
    assert payload["task_version"] == CALIBRATION["task_version"] == 2
    assert payload["case_facts"] == CASE["case_facts"]
    assert payload["thread"] == CASE["thread"]
    assert payload["reply"] == "An alternate answer."
    assert payload["style_target"] == {"words": 80, "word_count_is_a_gate": False}
    assert "Do not require a particular key prefix" in messages[0]["content"]


@pytest.mark.parametrize("change", [{"task_version": 1}, {"case_facts": []}, {"case_facts": [None]}])
def test_missing_v2_factual_standard_is_an_error(change):
    with pytest.raises(ValueError, match="versioned case facts"):
        judge.judge_messages({**CASE, **change}, "A reply.")


@pytest.mark.parametrize("criterion", judge.CRITERIA)
def test_every_criterion_is_required_for_pass(criterion):
    result = judge.evaluate_reply("A reply without deterministic violations.", {**PERFECT, criterion: 1})
    assert result["reward"] == 0
    assert result["failures"] == [f"criterion:{criterion}"]


@pytest.mark.parametrize("value", [True, "2", 2.0, 3, -1, None])
def test_invalid_correctness_score_cannot_pass(value):
    with pytest.raises(ValueError, match="every v2 criterion"):
        judge.parse_judgment(json.dumps({**PERFECT, "correct": value}))


def test_old_three_criterion_judgment_cannot_pass():
    old = {key: value for key, value in PERFECT.items() if key != "correct"}
    with pytest.raises(ValueError):
        judge.parse_judgment(json.dumps(old))
    assert "criterion:correct" in judge.evaluate_reply("A reply.", old)["failures"]


@pytest.mark.parametrize("reason", ["", "  ", None])
def test_judge_must_explain_the_scores(reason):
    with pytest.raises(ValueError, match="explain"):
        judge.parse_judgment(json.dumps({**PERFECT, "reason": reason}))


def test_word_target_is_diagnostic_not_a_gate():
    reply = "useful detail " * 45
    result = judge.evaluate_reply(reply, PERFECT)
    assert result["word_count"] == 90 > result["word_target"]
    assert result["passed"] is True
    assert all("word" not in gate for gate in result["gates"])


@pytest.mark.parametrize("separator", ["| --- | --- |", "--- | ---", "|:---|---:|", "| :---: | :---: |"])
def test_markdown_table_cannot_pass_even_with_perfect_model_scores(separator):
    result = judge.evaluate_reply(f"Kind | Use\n{separator}\nKey | API credential", PERFECT)
    assert result["failures"] == ["gate:no_markdown_table"]
    assert result["reward"] == 0


def test_inline_pipe_is_not_a_table():
    assert judge.evaluate_reply("A | B is just an inline label.", PERFECT)["passed"] is True


@pytest.mark.parametrize("reply,gate", [("", "reply_posted"), ("A\u2014B", "no_em_dash"), ("Hello \U0001f600", "no_emoji")])
def test_existing_hard_gates_are_preserved(reply, gate):
    assert f"gate:{gate}" in judge.evaluate_reply(reply, PERFECT)["failures"]


def test_execution_or_judge_error_always_fails():
    result = judge.evaluate_reply("An answer.", PERFECT, "Gateway unavailable")
    assert result["failures"] == ["judge:error"]
    assert result["reward"] == 0


def test_calibration_examples_cover_semantics_and_style_independently():
    by_id = {entry["id"]: entry for entry in CALIBRATION["cases"]}
    assert by_id["concrete_three_sentences"]["expected_pass"] is True
    assert by_id["alternate_wording_and_standalone_key"]["expected_pass"] is True
    for name in ("short_but_vague", "short_but_wrong_credential", "incorrect_mandatory_membership"):
        entry = by_id[name]
        assert len(entry["reply"].split()) < judge.WORD_TARGET
        assert entry["must_fail_criteria"] == ["correct"]
    assert by_id["padded_recap"]["must_fail_criteria"] == ["direct", "brief"]
    table = by_id["unneeded_table"]
    assert "gate:no_markdown_table" in judge.evaluate_reply(table["reply"], PERFECT)["failures"]


def test_cli_sends_v2_facts_and_records_v2_result(tmp_path, monkeypatch):
    captured = {}
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"posted_reply": CALIBRATION["cases"][0]["reply"]}))
    output, reward = tmp_path / "out/report.json", tmp_path / "out/reward.txt"

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(output_text=json.dumps(PERFECT))

    def client(**kwargs):
        captured["client"] = kwargs
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setattr(judge, "OpenAI", client)
    monkeypatch.setenv("LITELLM_API_BASE", "https://example.invalid")
    monkeypatch.setenv("LITELLM_API_KEY", "test-fixture")
    monkeypatch.setenv("MOYAI_EVAL_JUDGE_MODEL", "judge-fixture")
    monkeypatch.setattr("sys.argv", ["judge.py", "--case", str(TASK / "tests/case.json"), "--result", str(result_path),
                                   "--requests", str(tmp_path / "requests.json"), "--output", str(output), "--reward", str(reward)])
    judge.main()
    report = json.loads(output.read_text())
    assert report["task_version"] == 2
    assert report["passed"] is True
    assert report["judgment"] == PERFECT
    assert reward.read_text() == "1\n"
    assert captured["client"]["base_url"] == "https://example.invalid/v1"
    assert captured["model"] == "judge-fixture"
    assert json.loads(captured["input"][1]["content"])["case_facts"] == CASE["case_facts"]
