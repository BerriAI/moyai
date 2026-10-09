# Project environments

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Prepared project environments

Each repository added to Moyai's shared GitHub connection appears automatically
in **Environments**. Discovery uses the intersection of the configured allowlist
and repositories approved for that connection; it never expands GitHub access.
Registration does not allocate a machine. The first new session that selects the
repository or its environment queues a build and waits for validation, sharing
the same build with other waiting sessions. An admin can also prebuild it.
Temporal records the wait and uses timers without occupying a session sandbox
or sandbox-capacity slot; deployment does not lose that wait. Once the build
passes, matching sessions get isolated copies of its snapshot. Failed builds
produce an actionable session error and need a manual retry, avoiding retry loops.

Sessions without a repository use base tools unless an administrator explicitly
chooses a workspace default. LiteLLM is an optimized preset for `BerriAI/litellm`,
not a default injected into unrelated work. Existing session checkpoints, admin
edits, cancellations and disabled environments are preserved. New private-repo
sessions recheck the live GitHub connection before using a prepared snapshot.
Set `AUTO_PREPARE_REPOSITORIES=false` to opt out of automatic discovery.

Automatic setup recognizes root-level `uv.lock`, `pyproject.toml`, Python
requirements files, npm lockfiles and pnpm lockfiles with an exact `packageManager`
version. Frozen installs are used where a supported lockfile is present. Python
uses a separate `.venv`; Node uses an official binary verified against its SHA-256
checksum, with numeric `.nvmrc`/`.node-version` selectors or an LTS default.
Dependency integrity checks run before publication. These checks do not replace
the tests or application health checks required by an individual task.

Custom services or other stacks can commit `.moyai/environment.json`, for example:

```json
{
  "apt_packages": ["postgresql"],
  "setup": "./scripts/install-development.sh",
  "startup": "./scripts/start-development.sh",
  "verify": "./scripts/check-development.sh",
  "shutdown": "service postgresql stop",
  "instructions": "Use the local development database and run the project tests."
}
```

This file takes precedence over dependency detection. Commands execute inside
the isolated build with no model or app credentials. The resolved recipe is
saved with the build, so service startup survives session restoration. Admins
can instead choose **Custom commands** in the recipe editor. Devcontainer/Docker
execution, Yarn/Poetry and other unsupported runtimes need an explicit recipe;
the build reports that requirement instead of claiming their services are ready.
Source-only repositories get the checked-out source and base tools, with that
limited preparation stated in the agent's project instructions.

Admins can open **Environments** to create an organization recipe from the LiteLLM
starter or a custom project. Configure a GitHub repository and branch/tag/commit,
Debian packages, install commands, idempotent service startup, verification,
clean shutdown, and short project instructions. Choose **Public repository** for
public code, or **Shared GitHub connection** for repositories granted to the
existing GitHub App. Private clones use an ephemeral read-only installation
credential for Git only; model and connected-app credentials are not supplied to
the build or included in its snapshot. Recipes and their resulting source and
files are organization resources, not personal environments.

Save the recipe, then **Build environment**. Builds run one at a time in separate
sandboxes with a one-hour lifetime using the selected provider. Modal builds use 2 CPUs and 8 GiB; size the [Substrate template](substrate.md) for the dependencies being installed. The build log and exact source
SHA appear in the admin page. All install, startup, verification and shutdown
commands must succeed before a filesystem snapshot becomes available. Setup
runs in the sandbox provider, separately from the Moyai web host. Failed or cancelled builds leave the last
working environment in place. Editing during a build cannot publish the obsolete
recipe over a newer one. Named build sandboxes, a detached supervisor, and the
SQLite journal allow the web worker to reattach after deployment without rerunning
installation steps. Lost machines require a new build. Cleanup is retried.

Discovered environments are selectable under **Session setup** before their
first build, and become enabled after validation. Custom manual environments
must be built and enabled. **Automatic** matches an explicit repository; with no repository,
it selects the administrator's workspace default. This includes new Slack
sessions. **Base tools only** opts out. **Use by default** enables the environment
and selects it for sessions without a repository. Existing sessions keep their
checkpoints. Each session pins its selected build; subagents inherit it and the
parent's copied filesystem. Session snapshots take precedence over the clean
project snapshot, so rebuilding cannot erase ongoing work. Startup commands run
again after restore and must be idempotent; running processes are not preserved
by a filesystem snapshot. The session Activity panel identifies its environment
and source commit.

**Refresh daily and after recipe edits** is optional and off initially. When
enabled, the worker queues a rebuild daily and after edits, coalesces pending
builds, and retains the last successful version if refresh fails. Each build
fetches the configured ref and records the resolved commit. Source and dependencies
stay pinned within a session; after changing revisions for a task, the agent must
refresh dependencies in that checkout. Recipes can also be rebuilt manually.
Automatic builds consume Modal compute just like manual builds.

The LiteLLM starter installs a separate Python 3.13 project environment from
`uv.lock`, proxy/database dependencies, pytest and Playwright. The frozen install
builds the native extension with two Cargo workers and dev debug symbols disabled
to fit the 8 GiB build sandbox; the extension remains enabled. It initializes a
local PostgreSQL development database, applies `schema.prisma`, inserts 100 small
synthetic case records, and verifies database-connected readiness plus key
creation, lookup and deletion through the real proxy before stopping services
for the snapshot. Setup saves the local database URL in the repository's `.env`
when that file is absent, so worker commands retain it after a restore. An
existing `.env` is preserved. These fixtures are a starting point;
they are not production-representative data or model responses. Benchmarks should
create equivalent isolated datasets for the exact base/head revisions. The known
local database password in the recipe is solely for this sandbox's development
database; it is not a provider or production credential. Template setup is
editable because dependency requirements can change with the source revision.
Starter changes apply to newly created recipes. Existing saved recipes and
sessions stay pinned: an administrator must update the recipe and rebuild it,
then start a new session to adopt the revised preparation.
