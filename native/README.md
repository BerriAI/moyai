# Moyai runtime sources

Use the [root installation guide](../README.md) and root `uv.lock`. Run commands
from the repository root; `uv sync --frozen` installs the workspace into `.venv`.

- `src/moyai`: Pydantic AI/Harness composition, Temporal workflows, API and product adapters.
- `web`: the React application and its own assets; shared controls live in `../frontend`.
- `skills`, `instructions.md`, `desktop.py`, `browser_guest.py`, `browser_driver.py`: packaged runtime resources.
- `tests`: runtime, replay, authorization, migration, recovery and package coverage.
- `hatch_build.py`: packages the shared canonical `app` services with the runtime.

See [architecture](../docs/architecture.md), [usage](../docs/usage.md), and the
[deployment contract](../docs/pydantic-deployment.md). Build distributables with
`uv build --package moyai-pydantic`; wheels and source distributions include the
shared product services, historical readers and frontend assets.

Browser commands reuse an async Playwright worker inside the desktop guest.
`browser_driver.py` supervises that worker from a separate process and enforces
the request deadline and caller connection. A timeout, lost caller, worker error,
or idle expiry retires its private process group, including the Playwright driver.
Chromium and the desktop remain running; the next command starts a fresh worker.
The guest still serializes commands with `command.lock`, and application leases
and effect receipts still govern browser actions. A possibly dispatched action
is never replayed automatically. Recovery cannot undo an action already sent to
Chromium, so inspect an uncertain result before issuing another write.
