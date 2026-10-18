# Move an agent turn at a deployment checkpoint

Moyai can opt in to Temporal Worker Deployment Versioning and move a running
agent turn to an eligible release at a settled checkpoint. The workflow stays
pinned while a model request or tool batch is active. An operator requests a
checkpoint after qualifying the destination and setting Temporal routing.

This is the worker-runtime part of a release system. It does not create a second
application fleet, switch HTTP traffic, migrate schemas, transfer singleton
leadership, or authorize retiring the previous fleet.

## Configure a release identity

Set both values on the participating application roles:

```sh
MOYAI_WORKER_DEPLOYMENT=moyai-workers
MOYAI_WORKER_BUILD_ID=full_source_commit_sha
```

Use a real, immutable source revision for each build ID. The deployment name
stays the same across releases. A partial identity fails validation. Leaving
both empty preserves unversioned polling.

All four pollers register the same deployment version: the workflow queue and
its `-control`, `-model`, and `-tools` activity queues. Workflows default to
Temporal's pinned versioning behavior. Activity routing and workflow execution
must agree on the release.

For an existing distributed product, enabling this changes the reviewed native
compatibility contract. Build IDs may differ between compatible participants,
but fleet identity and handoff protocol must agree. Qualify the initial
unversioned-to-versioned adoption separately. Do not disable the unversioned
workers while their existing histories still need them.

The integration is exercised with the repository's locked Temporal Python SDK
and a real Temporal Server 1.32.0. Verify the target server and SDK support before
enabling it in another installation.

## Request a handoff

First verify the destination's application compatibility and every required
poller. For retirement, promote the intended version to current and remove any
ramp to an unintended version. The checkpoint selects Temporal's current/ramping
destination; the request does not name or bypass that routing policy.

Use an authorized Temporal client with the installation's existing Pydantic AI
plugin and encrypted payload converter:

```python
from moyai.deployment_handoff import DeploymentHandoff
from moyai.workflow import TurnWorkflow

handle = client.get_workflow_handle(workflow_id)
await handle.signal(
    TurnWorkflow.checkpoint_handoff,
    DeploymentHandoff(
        request_id=release_request_id,
        deployment_name="moyai-workers",
        source_build_id=previous_source_sha,
    ),
)
state = await handle.query(TurnWorkflow.deployment_checkpoint)
```

The signal is recorded in Temporal history. Its source identity prevents a
delayed request from moving an unrelated release, and unversioned workflows
ignore it. A repeated request already carried through a handoff is ignored.
The control client needs the existing namespace authorization; this creates no
public unauthenticated deployment endpoint.

At the next eligible `ModelRequestNode`, Moyai waits for steering preparation
to settle and saves the same encrypted checkpoint used for bounded-history
continuation. It retains conversation history, workspace reference, usage,
retry budgets, stream offsets, and pending steering. Pending tool effects and
unsettled gateway attempts prevent a deployment checkpoint. A refused or
unacknowledged save leaves the existing agent running.

After persistence, the old execution closes with continue-as-new and the new
execution selects the routed release. Ordinary history rollover continues to
retain its existing pinned version. The logical workflow ID and turn ID remain
unchanged; the Temporal run ID changes.

Inspect `build_id` and `previous_request_id` in the query result and the actual
Temporal execution history. Receiving the signal is not proof of a completed
handoff.

## Retirement and failure

Keep the source workers available until Temporal drainage and the application's
own ownership checks prove they are no longer needed. Other workflow types,
pending activities, and unresolved external outcomes can retain an old release.
A checkpoint request is not a blanket fleet-drain command.

Do not cancel a model/tool activity to force the checkpoint, replay an uncertain
external action, or force an incompatible snapshot into a new schema/framework.
If the destination is unavailable, retain the old fleet and inspect the
persisted workflow state before changing routing or retrying an operation.

## Verification

Run the focused regressions from the repository root:

```sh
uv run --frozen pytest -q \
  native/tests/test_deployment_handoff.py \
  native/tests/test_history_handoff.py \
  native/tests/test_worker_pool_shutdown.py
```

The two-release test uses a real local Temporal server and verifies that:

- Routing new work to green leaves an active blue model call pinned.
- A source-scoped request crosses exactly one settled checkpoint.
- Activities in each execution run on that execution's release.
- The six external file writes occur once each in order.
- Stream offsets, agent step IDs, usage, and workspace state survive.
- Pending or unknown gateway attempts refuse a deployment checkpoint.

Scripted-model regressions are supplementary. Release qualification must also
exercise the actual application with the configured gateway, real tools,
authenticated requests, streams, and the infrastructure being replaced.
