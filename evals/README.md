# Moyai agent evaluations

This directory contains the Harbor 0.24.0 evaluations, one task per real use case under `tasks/`. `deepseek_support_live` checks a support answer is proven live, and `answer_style` checks a plain explanation follows the Answer style rules in `native/instructions.md` (no em dashes, answer first, no filler). Harbor is installed as a temporary `uv` overlay so it does not change the native production dependency lock.

## Run

Set `LITELLM_API_BASE`, `LITELLM_API_KEY`, `MOYAI_EVAL_AGENT_MODEL`, `MOYAI_EVAL_JUDGE_MODEL`, and `MOYAI_TEST_POSTGRES`, then run from the repository root:

```sh
PYTHONPATH=.:evals:native/src:native/tests \
  uv run --project native --python 3.13 \
  --with harbor==0.24.0 --with 'moto[server]==5.1.4' --with openai==3.29.0 \
  harbor run -p evals/tasks/<task> \
  -a moyai_harbor.agent:MoyaiAgent -k 3 -n 1 -o evals/jobs -y
```

View the saved runs with:

```sh
uv run --project native --python 3.13 --with harbor==0.24.0 harbor view evals/jobs
```

## Add a task

Create a directory under `evals/tasks/` containing `task.toml`, `instruction.md`, an `environment/` with its Dockerfile and fixtures, and `tests/` with a `test.sh` verifier. Keep task-specific fixtures in that task's environment. The `moyai_harbor` package provides the Moyai agent and turn runner shared by tasks.

`evals/moyai_harbor/workspace.py` contains an eval-only `WorkspaceRef(provider="modal")` shim. It presents Harbor's isolated environment as the Modal workspace reference expected by credential execution, while forwarding commands to Harbor's environment. It is not a production workspace backend.
