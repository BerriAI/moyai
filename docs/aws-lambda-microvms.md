# AWS Lambda MicroVM sandboxes

Select **AWS Lambda MicroVMs** in **Settings → Runtime**, or set `SANDBOX_PROVIDER=lambda`. New sessions use your AWS account; existing Modal/Substrate sessions retain their original provider. Model and app credentials remain on the Moyai server. The AWS SDK uses the server's standard credential chain (workload role, environment, or an optional named profile).

## Prepare AWS

Use a region/account with Lambda MicroVM access, an S3 bucket in that region, and a private checkpoint prefix dedicated to this Moyai installation. Enable S3 Block Public Access and encryption. Keep the bucket, published checkpoints, and their pinned MicroVM image versions for as long as sessions or prepared environments reference them. Give each Moyai installation its own prefix.

The server needs `lambda:RunMicrovm`, `lambda:GetMicrovm`, `lambda:TerminateMicrovm`, `lambda:CreateMicrovmAuthToken`, and `lambda:GetMicrovmImageVersion`; `lambda:PassNetworkConnector` on the ingress and egress connector ARNs passed to `RunMicrovm`; S3 `GetObject`, `PutObject`, and `ListBucket` for the configured bucket/prefix; and `iam:PassRole` only if you configure a VM execution role. For the default networking in `us-east-1`, scope `lambda:PassNetworkConnector` to `arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS` and `arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS`. Substitute your region and configured egress connector when appropriate. Without this permission, AWS rejects the launch with `AccessDeniedException` even when `lambda:RunMicrovm` is allowed. The guest receives short-lived, object-scoped presigned S3 URLs, **not** the server's AWS credentials. Checkpoints and registry entries use SSE-S3 (`AES256`). Buckets requiring SSE-KMS need an integration change; they are not supported by this initial backend.

For building the image, the caller additionally needs `lambda:CreateMicrovmImage`, access to upload the build artifact, and `iam:PassRole` for the build role. The build role must trust `lambda.amazonaws.com` with `sts:AssumeRole` and `sts:TagSession`, and have S3 artifact-read and CloudWatch build-log permissions. Follow [AWS's setup guide](https://docs.aws.amazon.com/lambda/latest/dg/microvms-getting-started.html) for the exact trust policy and regional prerequisites.

Package the source without uploading anything:

```sh
uv run python scripts/lambda_image.py
```

To upload it and start an AWS image build:

```sh
uv run python scripts/lambda_image.py --create \
  --profile YOUR_TEST_PROFILE --region us-east-1 \
  --bucket YOUR_BUCKET --name moyai-workspace \
  --build-role-arn arn:aws:iam::123456789012:role/MicrovmBuildRole
```

Wait for the image to be ready with `aws lambda-microvms get-microvm-image --image-identifier IMAGE_ARN`. Save its **explicit image version**, not a mutable latest alias. The supplied image uses ARM64 and requests at least 8 GiB memory. AWS selects VM resources from the image; the provider's per-create `memory` argument cannot resize it. The image includes the same coding harnesses, browser tools, and package managers as the other Moyai backends. On each launch the guest restarts its Python interpreter before accepting work, discarding snapshotted TLS, random generator, and request state. No model connection or agent execution starts at image-build time.

Configure Moyai:

```dotenv
SANDBOX_PROVIDER=lambda
LAMBDA_REGION=us-east-1
LAMBDA_IMAGE=arn:aws:lambda:us-east-1:123456789012:microvm-image:moyai-workspace
LAMBDA_IMAGE_VERSION=1.0
LAMBDA_CHECKPOINT_BUCKET=YOUR_BUCKET
LAMBDA_CHECKPOINT_PREFIX=moyai-lambda
# Optional on developer machines; leave empty for workload-role credentials.
LAMBDA_PROFILE=YOUR_TEST_PROFILE
# Optional; ordinary guest operation does not need AWS account permissions.
LAMBDA_EXECUTION_ROLE_ARN=
# Optional custom network connector; default is INTERNET_EGRESS.
LAMBDA_EGRESS_CONNECTOR=
# Allow large dependency changes enough time to hash, archive and upload.
SNAPSHOT_TIMEOUT_SECONDS=600
```

The authenticated port-80 endpoint and outbound internet connectors are attached at launch. Outbound access must reach Moyai's `PUBLIC_URL`, S3, the configured model broker, and repository/package hosts. The guest HTTP server must stay behind AWS's authenticated ingress; never expose it directly to an untrusted network. The Runtime connection test runs a real harness-import command in a temporary VM and confirms termination before saving the provider selection. It does not certify the whole checkpoint/restore path; run the conformance test below too.

## Checkpoint and rotation behavior

AWS's [runtime APIs](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_Operations.html) expose suspend/resume of an existing VM, but no runtime snapshot-export/clone operation. [Image snapshots](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images-snapshots.html) capture the image build, not later session work. Moyai therefore stores independent filesystem checkpoints in S3.

Each checkpoint contains all new/changed files and deletions relative to its **pinned immutable base image**. It never depends on a previous runtime checkpoint. The guest hashes file contents, modes, ownership, links and extended attributes. Image-layer timestamp precision is normalized to seconds. Included data covers ignored/untracked files, `node_modules`, virtual environments, installed system packages and package databases, home directories, `/tmp/hermes-home`, and `/session` harness/native conversation journals. Kernel mounts, VM-private runtime machinery, network identity files and sockets/device nodes are excluded. The compressed delta currently has a **4 GiB limit**; exceeding it fails the save rather than dropping files. There must also be enough guest disk space for the temporary archive.

Commands and Computer input are blocked during checkpoint creation; guest command processes are quiesced during hashing/upload and resumed on the original VM. Restores apply the archive to a **new VM**, with a new endpoint and execution capability. They do not restart saved processes or replay command journals. An abandoned durable execution remains `uncertain`, which interrupts the task instead of executing it twice. Background processes are not portable; their files are saved, and they must be explicitly restarted when appropriate. The guest is a Linux child subreaper, so even detached commands with cleared environments remain part of its quiesced process tree.

S3 validates SHA-256 on upload. Moyai confirms checksum and byte count before atomically publishing a manifest; restoration checks the archive again before applying it. The completed answer is persisted separately before this work, so a failed save preserves that answer, leaves the previous checkpoint intact, and stops queued work. Temporary orphan archives from interrupted uploads can be removed after verifying that no manifest references them. Do not apply an age-based deletion policy to published checkpoints or the `machines/` provisioning registry while they are in use.

AWS's [maximum VM lifetime is eight hours](https://docs.aws.amazon.com/help-panel/lambda/latest/console/lambda-microvm-create-idle-policy.html). Moyai requests rotation at a safe tool-round boundary after at most **seven hours from AWS's original `startedAt`** (or the shorter configured rotation interval). This leaves an hour for the current tool round, up to three saves, and termination. Startup, follow-ups and worker reconnects do not reset the deadline. A replacement is never started until the old VM's termination is confirmed. A tool that itself runs longer than the remaining hour cannot be guaranteed a checkpoint; expiry is reported as interruption, with no automatic action replay.

AWS detects idle time from inbound endpoint traffic, which misses outbound model calls and background commands. This backend sets the idle-suspend threshold to the full maximum lifetime and disables auto-resume. It does **not** use suspension. Moyai's existing confirmed-idle lifecycle releases saved VMs and later restores their checkpoints. A named launch uses an S3 registry and a stable AWS idempotency token to recover a lost `RunMicrovm` response without launching a duplicate; unresolved creation older than one hour fails for operator inspection.

## Conformance tests

Local tests use the real full Linux guest image, shell/package processes, HTTP service, and a TLS S3 fixture. They simulate the AWS control plane; they do not validate AWS IAM, quotas, networking, image builds, or service behavior:

```sh
uv run pytest -q tests/test_lambda_microvms.py
docker build --target lambda-workspace -f Dockerfile.sandbox -t moyai-lambda:test .
uv run python scripts/lambda_smoke.py --local-image moyai-lambda:test
```

With AWS settings and permissions available, run the same conformance checks on actual MicroVMs. Use a disposable checkpoint prefix and grant the test caller `s3:DeleteObject` on that prefix so it can clean up its test checkpoints:

```sh
uv run python scripts/lambda_smoke.py --live
```

The test installs a small system package, runs the real SDK and MCP transport against local inference fixtures, builds a prepared GitHub project environment, and exercises Chromium clicks/screenshots. It then saves an active background writer, deletes the source VM, restores independent children, checks dependencies/conversation/permissions/symlinks/deleted files, starts a fresh Computer service in the restored VM, verifies no background process or uncertain execution is replayed, and confirms termination. It cleans up its VMs and published test checkpoints; provisioning registry entries are retained for diagnosing ambiguous creates. It makes no external model inference calls. If interrupted before AWS acknowledges a launch, inspect `machines/` and the MicroVM console before deleting registry entries.

The automated local workflow runs on ARM64, matching the AWS API's supported architecture. Live AWS validation is a separate release check.
