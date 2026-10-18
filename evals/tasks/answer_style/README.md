# Answer style, version 2

This task asks the same virtual-key-versus-team question as version 1. The factual
standard comes from the existing task's reference: keys authenticate proxy API
calls, while teams group members and keys under shared controls. Version 2 makes
that distinction explicit in `tests/case.json` and the judge rubric. It does not
add a required answer template, key prefix, or exact example.

The four criteria are `verdict_first`, `direct`, `correct`, and `brief`, each scored
0–2. All four must score 2 (8/8) to pass. The existing reply, em-dash, and emoji
gates remain; a Markdown table also fails. About 80 words is a style target,
**not a pass/fail cutoff**. A short answer can fail for vagueness or wrong facts;
a useful longer answer can pass. The criterion scores and judge reason explain
which distinction or unnecessary detail affected the result.

`tests/calibration.json` contains alternative correct answers and independent
negative examples: vague wording, incorrect credentials, mandatory team
membership, missing member/key grouping, repeated recap, and an unnecessary
table. Run these through `judge_messages`, `parse_judgment`, and `evaluate_reply`
with the configured judge model to check their expected verdict and any
`must_fail_criteria`/`must_fail_gates`. This uses only judge calls, not agent runs.
Keep measured calibration outputs with the benchmark artifacts, outside the task
fixtures. The focused deterministic tests are:

```sh
python -m pytest -q tests/test_answer_style_eval_v2.py
ruff check evals/tasks/answer_style/tests/judge.py tests/test_answer_style_eval_v2.py
```

Version 1 scored three criteria out of 6. Retain those results as version 1;
compare baseline and proposed prompts under the same version 2 judge rather than
treating the old and new totals as interchangeable.
