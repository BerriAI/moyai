# Local development in BerriAI/moyai

The root `pyproject.toml` and `uv.lock` own backend dependencies. CI generally uses
Python 3.13; the package declares Python >=3.12. From the checkout root:

```sh
uv sync --frozen --python 3.13
```

Use `uv run --frozen --python 3.13 ...` for Python commands. Do not install the
unrelated PyPI package named `agentchat`: this repository pins a Git revision.

For frontend work, read `.agents/skills/moyai-ui/SKILL.md` and `docs/shadcn-ui.md`.
The React/shadcn sources build into committed `app/static/ui` assets:

```sh
npm ci
npm run typecheck
npm run build
npm test
```

Inspect generated changes under `app/static/ui` and commit them alongside changed
frontend sources. Plain `app/static/*.js` controllers do not all require a bundle
rebuild; the current frontend workflow determines the required checks.

## Run the local app

`app.main:app` is the ASGI entrypoint. For an isolated, offline local preview,
use a new empty directory and clear inherited provider configuration. Existing
`scripts/*_demo.py` examples construct explicit `Settings` defaults for this
purpose. Never reuse a production `DATA_DIR`, database URL or object-storage bucket.

When the task already has a configured development environment:

```sh
uv run --frozen --python 3.13 uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Readiness is an HTTP response from `/api/session`, followed by the affected page
or API flow. A running process alone is insufficient. An offline preview can
exercise UI and local APIs; inference and cloud execution require their configured
connections. Follow the live tool's access/setup guidance if a real service is needed.

For existing demos, read their module docstring before running. Some use synthetic
upstreams and some launch real containers or cloud sandboxes. Keep those claims
distinct in the handoff, and stop only the processes started for this task.
