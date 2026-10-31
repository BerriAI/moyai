# Native Pydantic + Temporal on AWS Lambda MicroVMs

`SANDBOX_PROVIDER=lambda` selects an AWS Lambda MicroVM for new native computers.
Pydantic AI Harness and Temporal still own model calls, tools and durable
conversation execution on the trusted worker. The guest contains workspace tools
and a command/checkpoint transport. It contains no Hermes or Deep Agents runtime.
Modal remains the default; existing workspace references keep their provider.

Use an AWS account and region with MicroVM access, a private encrypted S3 bucket,
and a prefix dedicated to this installation. The worker uses the standard boto3
credential chain; guests receive only short-lived URLs scoped to one checkpoint
object. Do not place worker/model credentials in the image.

Build from the current checkout:

```sh
uv run python scripts/lambda_image.py --create \
  --region us-east-1 --bucket YOUR_PRIVATE_BUCKET \
  --name moyai-native-workspace \
  --build-role-arn arn:aws:iam::123456789012:role/MicrovmBuildRole
```

Without `--create`, this only packages the guest. The package selects ARM64 and
at least 8 GiB memory. The image includes the native coding/browser tools, Skills
and desktop scripts. Wait for `GetMicrovmImageVersion` to report `SUCCESSFUL` and
configure the explicit version:

```dotenv
SANDBOX_PROVIDER=lambda
LAMBDA_REGION=us-east-1
LAMBDA_IMAGE=arn:aws:lambda:us-east-1:123456789012:microvm-image:moyai-native-workspace
LAMBDA_IMAGE_VERSION=1.0
LAMBDA_CHECKPOINT_BUCKET=YOUR_PRIVATE_BUCKET
LAMBDA_CHECKPOINT_PREFIX=moyai-native-lambda
SNAPSHOT_TIMEOUT_SECONDS=900
MOYAI_COMPUTER_TIMEOUT=28800
MOYAI_COMPUTER_IDLE_TIMEOUT=600
# Optional:
LAMBDA_PROFILE=
LAMBDA_EXECUTION_ROLE_ARN=
LAMBDA_EGRESS_CONNECTOR=
```

Run the ordinary native entrypoints (`uv run moyai dev`, or the distributed
API/worker with Temporal). Docker installations accept the same configuration
through their environment file or `moyai.sh configure --from-env`.
The deployment check verifies the pinned image and bucket; it does not claim
that a complete checkpoint/restore conformance run has passed.

The worker needs MicroVM run/get/resume/terminate/auth-token/image-version APIs and
`lambda:PassNetworkConnector` for its selected ingress/egress connectors. Scope
S3 get/put/list permissions to the installation bucket/prefix. Building additionally
needs image creation, build-artifact upload and `iam:PassRole` for a build-only
role trusting `lambda.amazonaws.com` with `sts:AssumeRole` and `sts:TagSession`.
That role needs artifact reads and its image's CloudWatch build-log writes.
The default network connectors are the region's `ALL_INGRESS` and
`INTERNET_EGRESS`. Guest HTTP must remain behind AWS authenticated ingress.
Checkpoints currently use SSE-S3 (`AES256`); SSE-KMS buckets are not supported.

## Image baseline and checkpoints

The first guest boot records its immutable baseline **after image layers have
materialized**, before reporting image readiness. Its one-time readiness budget
is 1800 seconds. Restored VMs re-exec the transport interpreter before accepting
work and never regenerate the baseline over runtime files.

Every checkpoint independently describes changes relative to that pinned image.
It includes installations, package databases, home/workspace files, deletions,
links, ownership, mode and extended attributes. Kernel mounts, instance networking
identity and private transport state are excluded. Processes are quiesced while
packing; restoration imports files into a fresh VM without replaying commands or
starting saved processes.

Restoration applies the exact archived xattr set after ownership and mode changes,
including Linux ACLs and file capabilities. Attributes removed during the session
are removed on restore as well. Matching attributes are preserved without first
deleting them: AWS's mandatory SELinux labels cannot be removed by guest root.
Build a new guest image with this restoration behavior for new computers. An
application-image upgrade alone does not replace guest code, and existing
checkpoints continue to select their original pinned guest image/version.

Content hashes can be reused only when device/inode, nanosecond ctime/mtime, size,
mode, ownership and link count still match. Recent timestamps and backwards clock
steps invalidate cache hints. Missing, malformed or mismatched image seeds fall
back to hashing. Metadata and hard-link relationships are compared on every save.
A cache hit never removes a persistent runtime addition from later archives.

Packing reports inventory, signature, archive and checksum timings and counts of
hashed bytes/cache hits. The worker logs preparation, upload, verification,
publication and total checkpoint durations separately. Image building and VM
setup are separate costs. S3 verifies checksum/length before manifest publication;
the native settlement transaction checks the original scope and accepted answer
again before publishing its checkpoint reference.

Keep pinned images, checkpoint objects and `machines/` launch receipts while
sessions refer to them. Archives have a 4 GiB compressed limit and need temporary
guest disk space. A failed/oversized save preserves the answer and reports that
automatic restoration is unavailable. No blank replacement or uncertain command
replay is used to recover a missing computer.

### Native suspend/resume and durable recovery

New Lambda computers use AWS native idle suspension. `MOYAI_COMPUTER_IDLE_TIMEOUT`
(default 600 seconds, capped at eight hours) controls the endpoint inactivity
window. During an admitted turn and its checkpoint save, Moyai sends read-only
health probes to keep the computer active, including while the model is thinking.
Once the turn settles, probes stop and AWS can suspend the VM. Background processes
alone do not count as endpoint activity; they pause when the VM suspends.

The next command resumes the **same VM**, retaining memory, running processes,
command receipts and disk. The guest suspend hook flushes files; the resume hook
checks initialization and discards cached filesystem hash hints without restarting
the interpreter. Moyai refreshes its endpoint token after explicit resume and opens
fresh transport connections. Applications running inside the sandbox must reconnect
their own external sockets after resume. Never replay an uncertain command to wake
the computer.

VM lifetime follows `MOYAI_COMPUTER_TIMEOUT` (default one hour), capped at eight
hours **including suspended time**. Suspension does not extend the deadline.
Completed turns still publish independent, verified S3 filesystem checkpoints.
After termination or expiry, the next admitted turn restores the latest accepted
checkpoint into a new VM from its pinned image. Files and installed packages survive;
RAM and running processes do not. An interrupted, uncheckpointed turn is not recovered
by replaying its commands. Keep S3 objects and pinned image versions for as long as
their sessions must remain recoverable.

Build and select a new guest image to enable the suspend/resume hooks. Existing VM
idle policies and checkpoint image versions are immutable for this integration;
upgrading only the application does not retrofit them. AWS documents the
[native lifecycle and eight-hour limit](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html).

Prepared environment snapshots remain provider-specific: a Modal environment
image cannot be used as a Lambda image. Plain repository preparation and native
file continuations use the selected provider. Interactive desktop streaming is
currently Modal-only; Lambda supports native shell/file/browser tools and
authenticated desktop snapshot/actions.

## Deploy the application on AWS

Lambda MicroVMs host the untrusted computers. The trusted Moyai application still
needs a continuously running API, coordinator, broker and Temporal worker. The
simplest single-host installation uses the Docker split bundle on EC2. An existing
EKS installation can use the same Lambda settings through the Helm chart's
`config` and runtime Secret. Neither path needs a Firecracker host or a Lambda
function wrapper.

| Component | AWS deployment |
| --- | --- |
| Application | One EC2 host running the split Docker bundle, or the published Helm chart on EKS |
| HTTPS | ALB + ACM certificate + DNS; preserve Host and allow streaming/WebSockets |
| Durable application data | PostgreSQL (normally RDS) and private S3 object prefixes |
| Durable execution | Existing Temporal service, or bundled Temporal with persistent storage for an evaluation |
| Untrusted computers | Pinned Lambda MicroVM image and a separate S3 checkpoint prefix |
| Installation keys | Persistent protected `moyai-state` volume/PVC, backed up together with the database |
| Model access | Reachable Responses-compatible gateway, matching API key and enabled model |
| Slack | Dedicated installed app, signing secret, OAuth credentials and sender allowlist |

For an evaluation, bundled PostgreSQL/Temporal on encrypted EBS can reduce setup,
but that is a single host with no automatic database failover. The shipped bundle
includes Temporal's database; bring a separate application PostgreSQL database.
Use RDS and your existing Temporal service for production. Do not expose either
database or Temporal ports through the ALB.

### Provision and connect

1. Choose an account/region with Lambda MicroVM access. Check the actual API,
   not ordinary Lambda function availability. Build the guest using the command
   above and wait for its exact version to become `SUCCESSFUL`.
2. Create the application's database and separate object/checkpoint prefixes.
   Block public S3 access and retain pinned images as long as checkpoints refer
   to them. Checkpoints currently require SSE-S3, not an SSE-KMS-only policy.
3. Attach a workload role to the application host. On EC2 require IMDSv2; Docker
   bridge containers need a metadata response hop limit that permits the bridge
   (typically 2). Limit access to the trusted application containers. Never attach
   this role to the untrusted MicroVM guest or copy model credentials into it.
4. Terminate HTTPS at your ingress. Allow the host's proxy port only from the ALB
   security group. Prefer SSM to exposing SSH. Register the public URL before
   initialization; changing it later changes installation identity.
5. Ensure the worker can reach PostgreSQL, S3, Temporal, the model gateway and
   AWS MicroVM control/authenticated data endpoints. A working laptop browser
   session does not establish server-to-server connectivity. Private gateways
   need working VPC routes/DNS and matching credentials before initialization.
   Slack events need a reachable signed webhook endpoint; interactive employee
   SSO in front of that endpoint prevents Slack delivery.

The runtime role needs these actions on the selected image ARN:
`lambda:RunMicrovm`, `lambda:GetMicrovm`, `lambda:TerminateMicrovm`,
`lambda:ResumeMicrovm`, `lambda:CreateMicrovmAuthToken`, `lambda:GetMicrovmImageVersion`.
Operators testing explicit suspension additionally need `lambda:SuspendMicrovm`.
Also allow `lambda:PassNetworkConnector` only on the selected connectors. Allow
`s3:ListBucket` on the selected buckets (needed by `HeadBucket`) and object
read/write permissions on the installation prefixes. Application object storage
may also require deletion for its own lifecycle. No image-build permissions or
`iam:PassRole` are needed by the runtime when `LAMBDA_EXECUTION_ROLE_ARN` is empty.
If supplying an execution role, scope `iam:PassRole` to that role and review its
guest permissions separately. AWS documents the [MicroVM IAM model and resource
scoping](https://docs.aws.amazon.com/lambda/latest/dg/microvms-security.html).

From a matching release bundle on the host, supply database/model/Slack secrets
through your secret manager or private environment, then configure:

```sh
export MOYAI_INSTALLATION_ID=moyai
export MOYAI_INSTALL_TOPOLOGY=split
export MOYAI_PUBLIC_URL=https://moyai.example.com
# A remote ALB needs the proxy bound to a reachable interface, protected by its SG.
export MOYAI_BIND_ADDRESS=0.0.0.0
export OBJECT_STORAGE_BUCKET=YOUR_PRIVATE_BUCKET
export OBJECT_STORAGE_REGION=us-east-1
export SANDBOX_PROVIDER=lambda
export LAMBDA_REGION=us-east-1
export LAMBDA_IMAGE=arn:aws:lambda:us-east-1:123456789012:microvm-image:moyai-native-workspace
export LAMBDA_IMAGE_VERSION=1.0
export LAMBDA_CHECKPOINT_BUCKET=YOUR_PRIVATE_BUCKET
export LAMBDA_CHECKPOINT_PREFIX=moyai/lambda
export MOYAI_COMPUTER_TIMEOUT=28800
export MOYAI_COMPUTER_IDLE_TIMEOUT=600
export MOYAI_TEMPORAL_MODE=bundled
export SLACK_BOT_ENABLED=true
export SLACK_SESSION_USERS=YOUR_SLACK_USER_ID
export GOOGLE_ALLOWED_DOMAINS=example.com
# Also supply MOYAI_DATABASE_URL, LITELLM_API_BASE, LITELLM_API_KEY, AGENT_MODEL,
# SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, and SLACK_SIGNING_SECRET privately.
sh moyai.sh configure --from-env --secrets-from-env --topology split --temporal bundled
sh moyai.sh up
sh moyai.sh check
sh moyai.sh status
```

For external Temporal, select `--temporal external` and supply its address,
namespace, TLS and credentials. In environment-secret mode, the service manager
must supply secrets on subsequent starts/upgrades too. To store supplied secrets
in private mode-0600 `.env` instead, omit `--secrets-from-env`. Never source that
raw Compose file as a shell script. Existing installations are not overwritten:
add the Slack/Lambda values explicitly, then use the documented maintenance
upgrade procedure to change images. See [Docker installation and
upgrades](../deploy/docker/README.md).

### Slack and checkpoint/restore acceptance

Use a dedicated test Slack app and isolated data for evaluation. In split mode,
configure OAuth and install through **Connections → Slack**; a raw bot token does
not substitute for the canonical workspace installation. Use the application's
requested scopes. Register `/oauth/slack/callback` as the redirect URL,
`/hooks/slack/events` for events and `/hooks/slack/interactions` for interactions,
including any configured base path. Subscribe to app mentions and messages in
the conversations the bot is intended to handle. Start with an explicit
`SLACK_SESSION_USERS` allowlist and enable only the required DM/thread surfaces.
With identity linking enabled (the default), also set `GOOGLE_ALLOWED_DOMAINS`
to the approved Slack members' email domains. Despite its name, this setting
also governs Slack profile eligibility; an empty value rejects Slack senders
even when their IDs are allowlisted. This does not require enabling Google OAuth.

`moyai.sh check` proves service connections and the pinned image's readiness. It
does not launch a computer, validate Slack delivery or prove restoration. Before
calling the deployment ready:

1. Sign in to the actual app. Verify anonymous application APIs are denied and
   an invalid Slack signature is rejected.
2. In an approved Slack test conversation, ask the installed bot to create a
   uniquely named file with known contents. Verify real model/tool execution,
   progress/final delivery in the same Slack thread and a completed checkpoint.
   Reply in-thread and verify it continues the same Moyai session.
3. Let the completed session idle until AWS reports `SUSPENDED` (use a 60-second
   idle timeout for the test). Reply in the same Slack thread. Verify the same VM
   ID resumes and both the file and an in-memory marker in a background test
   process survive. Verify no replacement VM or S3 restore occurred. Leave a model
   turn active longer than the idle window and verify keepalive prevents suspension.
4. Record the session's computer ID and checkpoint reference. Terminate only
   that test MicroVM using the AWS API. Send a follow-up asking the agent to
   **read** the existing file. Verify a different MicroVM ID, the saved contents
   and a new checkpoint, without asking it to recreate the file.
5. Verify installed packages, deletions, symlinks and permissions if the workload
   relies on them. Checkpoint restoration imports filesystem state into a fresh
   VM; it does not resume running processes.
6. Repeat with a short configured lifetime to exercise AWS expiry, not just manual
   termination. This reaches the same termination boundary as the eight-hour cap
   without claiming an eight-hour soak test. Verify that the new VM restores files
   but the old in-memory process is gone.
7. Restart the trusted application while retaining its database, object storage
   and installation volume. Verify sign-in, the same session/history and another
   real follow-up. Check failed and duplicate Slack delivery handling separately.

Record source revisions, image ARN/version, actual VM IDs, checkpoint hashes,
results and timings. Keep credentials and presigned URLs out of evidence. A
provider-level checkpoint test is useful, but does not replace the full Slack
workflow. Clean up temporary test VMs, hosts, load balancers and DNS after the
evaluation; retain application state and checkpoint objects if the installation
will be continued.
