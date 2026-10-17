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

The worker needs MicroVM run/get/terminate/auth-token/image-version APIs and
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
are removed on restore as well.

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

Lambda does not use suspension. VM lifetime follows `MOYAI_COMPUTER_TIMEOUT`,
capped at eight hours; `MOYAI_COMPUTER_IDLE_TIMEOUT` applies only to Modal.
Completed turns save checkpoints, while the VM remains available until explicit
termination or lifetime expiry.

Prepared environment snapshots remain provider-specific: a Modal environment
image cannot be used as a Lambda image. Plain repository preparation and native
file continuations use the selected provider. Interactive desktop streaming is
currently Modal-only; Lambda supports native shell/file/browser tools and
authenticated desktop snapshot/actions.
