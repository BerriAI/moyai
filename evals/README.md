# Moyai agent evaluations

This directory contains the Harbor 0.24.0 evaluations, one task per real use case under `tasks/`. `deepseek_support_live` checks a support answer is proven live, and `answer_style` checks a plain explanation follows the Answer style rules in `native/instructions.md` (no em dashes, answer first, no filler). Harbor is installed as a temporary `uv` overlay so it does not change the native production dependency lock.

## Interactive local chat

Run the same Moyai agent in a browser, with real conversation continuity, clarification answers,
and expandable tool-call traces. Docker hosts a disposable test database and the existing
DeepSeek fixture workspace; no Slack messages are sent. The provider responses and repository
files are fixtures, while agent inference uses your configured model gateway.

From the repository root:

```sh
uv sync --frozen --python 3.13
npm ci
npm run build:eval-chat
uv run --frozen python scripts/local_agent_chat.py
```

Put `LITELLM_API_BASE`, `LITELLM_API_KEY`, and `MOYAI_EVAL_AGENT_MODEL` in `.env`
(or use the existing `AGENT_MODEL`). Pass `--env-file /path/to/private.env` to use another file.
Docker must be running; `DOCKER_CONTEXT` is respected. No Modal credentials, judge model,
or manually configured PostgreSQL server are needed. The first start may build the test image.

Open **http://127.0.0.1:8798**. Use **New chat** for a separate conversation, send the
DeepSeek question, and try a follow-up. Answer clarification questions by choosing an option
or typing in the question’s answer field.
Only one reply runs at a time. New chats share the same fixture computer; they have separate
agent conversations. Refreshing the page preserves the running session. Restarting the server
starts fresh conversations and a fresh computer; transcripts are saved under
`.data/local-agent-chat/transcripts` for inspection, not resumed after restart.

The server binds only to loopback and rejects cross-origin writes. Keep credentials in the
environment file; they are never sent to the browser. Stop with Ctrl+C to remove the test
containers and database. Use `--port` or `--data-dir` to change the local listener or output folder.

For local development and checks:

```sh
npm run typecheck:eval-chat
npm run build:eval-chat
npx playwright install chromium
npm run test:eval-chat
PYTHONPATH=.:evals:native/src:native/tests \
  uv run --frozen --project native --with harbor==0.24.0 \
  --with openai==3.29.0 --with litellm==1.107.0.dev3 python -m pytest -q evals/tests
uv run --frozen ruff check evals/moyai_harbor evals/tests scripts/local_agent_chat.py
```

## Run

Set `LITELLM_API_BASE`, `LITELLM_API_KEY`, `MOYAI_EVAL_AGENT_MODEL`, `MOYAI_EVAL_JUDGE_MODEL`, and `MOYAI_TEST_POSTGRES`, then run from the repository root:

```sh
PYTHONPATH=.:evals:native/src:native/tests \
  uv run --project native --python 3.13 \
  --with harbor==0.24.0 --with openai==3.29.0 --with litellm==1.107.0.dev3 \
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
