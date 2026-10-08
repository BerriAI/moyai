# Cloud setup and deployment

[Documentation](README.md) · [Project overview](../README.md)

For a new installation, follow [Install Moyai and run your first real task](getting-started.md). Use this reference to operate the service or choose another host. The dated reports below record checks on earlier deployments.

## Enable cloud runs

Modal is the default sandbox provider. To connect an existing Substrate cluster, follow [Substrate sandboxes](substrate.md). The web app deployment and persistent data directory are independent of the sandbox provider.

With `deploy_modal.py`, you use the HTTPS URL assigned by Modal. Follow the [walkthrough](getting-started.md) for that setup. Set the values below if you host the web app on another service or a VM.

The cloud sandbox calls back to this server for model and app tools, so `PUBLIC_URL` must be a **reachable HTTPS address**. Loopback URLs deliberately keep cloud execution disabled.

Set these in `.env` or your host's secret store. For Substrate, replace the two Modal credentials with the [Substrate connection settings](substrate.md#environment-configuration):

```dotenv
PUBLIC_URL=https://your-workspace.example.com
WORKSPACE_PASSWORD=<at-least-16-characters>
MODAL_TOKEN_ID=<your-modal-token-id>
MODAL_TOKEN_SECRET=<your-modal-token-secret>
LITELLM_API_BASE=https://your-gateway.example.com/v1
LITELLM_API_KEY=<a-dedicated-budget-limited-key>
AGENT_MODEL=<your-gateway-model-name>
```

Also set stable `SESSION_SECRET` and `ENCRYPTION_KEY` in cloud deployments. The example file includes generation commands. If omitted, they are generated privately under `DATA_DIR`; preserve that directory together with the database.

Restart the server and check **Runtime**. Cloud mode becomes selectable only when configuration is complete. The first run builds and caches the Hermes image and may take several minutes. This readiness check confirms configuration, not successful authentication or a completed image build.

The default Modal sandbox uses container isolation. Set `MODAL_VM_RUNTIME=true` to opt into Modal's VM runtime beta when a workload needs a full Linux kernel. This flag does not install Docker or provision nested VMs for you.

### Deploy the control plane

The included `deploy_modal.py` hosts the browser app on a **Modal Server** in the same workspace as the agent sandboxes:

```sh
uv run python deploy_modal.py
```

The deploy command reads `.env`, generates missing workspace/session/encryption secrets privately, updates the dedicated `hermes-workspace-config` Modal Secret, and deploys `moyai`. It prints the actual HTTPS URL. The app resolves that URL on startup and validates Modal's forwarded hostname against that exact origin. Keep one server container (`min_containers=1`, `max_containers=1`) and the `recreate` deployment strategy. The product name and host are Moyai; internal secret/volume names retain the original `hermes-workspace` prefix to preserve credentials and history. When migrating to another app name, first stop the old app and verify its containers have exited before deploying a new writer against the same volume. This is an always-on service and incurs Modal usage while deployed; stop it in the Modal dashboard when no longer needed.

SQLite runs on the container's local disk. Complete database snapshots and the latest per-session result archives are committed to the `hermes-workspace-state` Modal Volume. API mutations are checkpointed before acknowledgement, and background activity is checkpointed every two seconds. A hard failure can lose the newest background events. Starting a replacement restores the last snapshot and interrupts unfinished tasks without replaying external writes. Do not scale the service above one container or use rolling deployments; a distributed worker/database design is needed for multiple writers. Redeployments interrupt active tasks.

Track your Modal token's expiry and replace it in the deployment's secret store and deployment `.env` before it expires. Redeploy with no active sessions. For a team installation, follow your organization's service identity policy.

### Alternative: Docker on an existing cloud host

The included Docker image runs the control plane on an always-on cloud VM or container host. The selected Modal or Substrate provider supplies the task sandboxes separately.

```sh
docker compose up --build -d
```

The compose port binds only to the cloud host's loopback interface. Place an HTTPS reverse proxy in front of port 8787, set `PUBLIC_URL` to its exact origin, and preserve the incoming Host header. Allow `/broker/` traffic from your sandbox provider with its run-scoped bearer tokens. Disable proxy buffering for event streams. Set an appropriate body-size limit (5 MB) at the proxy.

Use one replica with a persistent local disk. Avoid serverless request hosts that stop background work after an HTTP response. The Docker image was built and its task creation and restart persistence were verified locally. This alternative has not been deployed to a separate VM.

The container entrypoint prepares `DATA_DIR` after the disk is mounted, then drops to the `workspace` user (UID/GID 10001) before starting Python. It preserves file permissions and contents, adopts ownership only within the data directory, and does not follow symlinks to outside files. Use a dedicated absolute data directory. This also handles disks created by a previous native Python service with a different UID. Hosts that enforce a non-root container user must provision the disk for that user in advance. Do not override the entrypoint. The server and Docker health check both honor `PORT` (default 8787 outside Render).

Run maintenance, replay and database inspection commands as the database owner too. Docker exec and Render Web Shell can start as root without passing through the entrypoint. Even read-only SQLite connections can create WAL/SHM files briefly inaccessible to the running server, interrupting active model streams. `Store` rejects a different effective UID before opening the database; raw SQLite tools must follow the same rule. Keep the private file permissions intact.

For the standard image, use `docker exec --user 10001:10001 <container> /app/.venv/bin/python <script>` or, in Render Web Shell, `runuser -u workspace -- /app/.venv/bin/python <script>`. Verify `id -u` under that user before accessing live data. This does not require restarting the service.

To reproduce the migration and restart checks with disposable local Docker volumes:

```sh
docker build -t moyai-docker-smoke .
python scripts/docker_smoke.py moyai-docker-smoke
```

This seeds synthetic sessions, encrypted connections, keys, and an archive as UID 1000 with private permissions, reproduces the old SQLite permission failure, and verifies that the new image preserves the data and can write after a restart. It also checks non-root execution, Render's empty-database guard, staging mode, and ordinary Docker startup. No cloud credentials are needed.

## Render web app with Modal sandboxes

For an identity gate and an origin without a public Render endpoint, follow [Cloudflare Access and a private Render origin](cloudflare-access.md). That opt-in guide preserves Google sign-in and broker authorization and stages a separate private service before migrating current data.

Render also supports Moyai with [Substrate sandboxes](substrate.md). Set `SANDBOX_PROVIDER=substrate` in Render to skip the Modal prebuild; the Substrate cluster runs separately. The deployment and migration history below describes the existing Modal installation.

**Live migration verified September 29, 2026:** all 12 existing sessions, 14 messages, three organization connections, saved filesystem snapshot IDs, Slack source context, and 10 byte-identical result archives moved to Render. All three provider health checks passed. The existing continuity chat resumed on a new Modal sandbox and recovered “blue lantern” and file value `12`. A real [#bot-spam thread mention](https://berriaillm.slack.com/archives/C0B302ZJU05/p1790733863830609?thread_ts=1790733854.157109&cid=C0B302ZJU05) created a Render session (`b371989dcc8842fdad936f5784beec7f`), automatically read two source messages, and answered the marker `river-stone-73`. No external writes were requested. Both sandboxes terminated. The old Modal web deployment is stopped; its Volume remains a frozen migration backup. Existing workspace passwords are unchanged. A second Render deployment, with bootstrap disabled, preserved all 13 current sessions, 18 messages, 11 archive checksums, saved snapshots, and organization connections. Unauthenticated APIs still returned 401, and no Modal sandbox remained running.

`render.yaml` defines the company Render Docker service in Oregon with 1 CPU / 2 GB memory and a 10 GB persistent disk ($27.50/month base plus usage). Render hosts the browser UI, encrypted app connections, Slack webhook, SQLite history, and tool broker. Agent machines, filesystem snapshots, and Chromium still run in Modal. Service automatic deploys and Blueprint automatic synchronization are disabled because deployments interrupt active chat turns; check for active sessions before deploying. Manually sync the Blueprint after reviewing configuration changes, then deploy the intended commit. Keep one web instance. Render's disk forces stop-before-start deployments, preserving the single-writer database requirement.

Render builds `./Dockerfile` with context `.`. Leave **Docker Command** empty so the image's entrypoint can prepare the persistent disk and run `render_start.py`. Keep the disk mounted at `/var/data`, `DATA_DIR=/var/data/moyai`, and the health check at `/health`. The Render startup path derives the public origin from `RENDER_EXTERNAL_URL`, listens on `PORT` (default 10000), and retains the migration and empty-database guards.

The **Pre-Deploy Command** is `/app/.venv/bin/python /app/build_workspace_image.py`. It uses the same image recipe as new sessions, installs all harness dependencies, and verifies runtime imports before the replacement web service starts. Failed Modal prebuilds fail the deployment. Dependency layers are cached separately from agent code. Existing services must sync the Blueprint or set this command in the Dashboard; editing this file alone does not change a manually configured service. Modal credentials are read from the runtime environment during pre-deploy, never passed as Docker build arguments or baked into image layers. The check does not need the persistent disk, open the production database, or start an agent session.

For other hosts, run `uv run python build_workspace_image.py` with the deployment's Modal settings before deploying. This builds the image in Modal and incurs build usage; it does not deploy or restart the web service. If a later image build fails during provisioning, the affected response ends with a visible image-build error instead of retrying indefinitely. Fix and rebuild the image before sending a new message.

For a full build and short-lived sandbox check, run `uv run python scripts/workspace_image_smoke.py`. It checks Hermes and Claude SDK imports, the prepared harness runtime, and the Codex/OpenCode binaries without model calls, then terminates the sandbox. It does not deploy or restart the web service.

The deployed Blueprint sets `RENDER_MIGRATION_STAGE=false` and leaves `BOOTSTRAP_MODAL_VOLUME` empty now that the import is complete. For a fresh migration, `render_start.py` defaults to staging mode unless explicitly configured. The health endpoint is available, but sessions and Slack events are refused until cutover. Configure the existing environment secrets privately in Render; preserve `ENCRYPTION_KEY`, both workspace passwords, and `SESSION_SECRET`. `PUBLIC_URL` comes from Render's own `RENDER_EXTERNAL_URL`; Modal proxy rewriting and Modal Volume checkpoint writes are disabled on Render.

For cutover, wait for every old session to settle, stop the old Modal web service cleanly, then set `RENDER_MIGRATION_STAGE=false` and deploy on Render. On the first live startup, `BOOTSTRAP_MODAL_VOLUME=hermes-workspace-state` copies the final SQLite checkpoint and its result archives onto the Render disk. It validates the database, rejects checkpoints with active runs, and publishes the database only after all archives transfer. Existing Render data is never overwritten. After a successful import, remove the bootstrap environment variable to prevent accidentally restoring stale data onto a replacement disk.

Update the Slack Events request URL and Slack/Notion OAuth redirect URLs to the new origin, then verify a real Slack mention and an existing chat follow-up. Keep the original Modal Volume as a migration backup. If reverting after accepting new work on Render, export the current Render database and artifacts first; the frozen old Modal checkpoint is no longer current.

### Private file storage

SQLite stores chat history and file metadata. Configure a private S3-compatible bucket to store new attachments, previews, result archives, browser captures, and operator backups outside the Render disk. Existing inline and disk files remain readable during rollout. Without a bucket, local storage behavior remains enabled. Follow [the object-storage rollout guide](object-storage.md) for credentials, verified migration, backups, and rollback limits. The 10 GB Render disk provides database headroom; it does not replace object storage or backups.
