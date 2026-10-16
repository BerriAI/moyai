# Moyai runtime sources

Use the [root installation guide](../README.md) and root `uv.lock`. Run commands
from the repository root; `uv sync --frozen` installs the workspace into `.venv`.

- `src/moyai`: Pydantic AI/Harness composition, Temporal workflows, API and product adapters.
- `web`: the React application and its own assets; shared controls live in `../frontend`.
- `skills`, `instructions.md`, `desktop.py`, `browser_guest.py`: packaged runtime resources.
- `tests`: runtime, replay, authorization, migration, recovery and package coverage.
- `hatch_build.py`: packages the shared canonical `app` services with the runtime.

See [architecture](../docs/architecture.md), [usage](../docs/usage.md), and the
[deployment contract](../docs/pydantic-deployment.md). Build distributables with
`uv build --package moyai-pydantic`; wheels and source distributions include the
shared product services, historical readers and frontend assets.
