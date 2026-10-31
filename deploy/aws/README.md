# Deploy Moyai on AWS

This quickstart provisions a **new, single-instance installation** in your AWS
account. Moyai, Temporal, its LiteLLM gateway, Bedrock inference, storage and
Lambda MicroVM sandboxes run on AWS. Slack remains the external messaging service.
No Modal token, external model API key or Temporal Cloud account is required.

Terraform creates a separate VPC, two public/private subnet pairs, one NAT
gateway, HTTPS ALB/ACM/DNS, a private EC2 host, RDS PostgreSQL, private versioned
S3, scoped IAM roles, an empty Secrets Manager secret, CloudWatch logs, and an
encrypted EBS state volume with daily AWS Backup recovery points.
It also creates a private ECR repository for an organization-built application image.

The existing Docker split bundle runs the API, coordinator, broker and worker.
Temporal and its PostgreSQL database use the retained EBS volume. The application
uses RDS through a separate database login with verified TLS. The LiteLLM gateway
is reachable only on the application's Docker network and invokes the explicitly
allowed Bedrock model/profile through the host role. Guests have no execution
role and do not receive model, database or Slack credentials.

This is **not HA**: there is one application/Temporal host and one NAT gateway.
Do not scale the host or copy its live installation to another active worker.
Use the existing Helm deployment with separately qualified database/Temporal
services when availability requirements exceed this layout. This quickstart
does not implement a private-only/air-gapped deployment or an AWS Marketplace offer.
It needs outbound access for images, image builds, Slack and AWS APIs.

## Prerequisites

- Terraform 1.9+ (before 2.0), `uv`, a current checkout, and AWS credentials for
  the intended account. Run `aws sts get-caller-identity` before provisioning.
- An existing **public Route 53 hosted zone** in that account and an unused
  hostname under it. No existing application DNS record is overwritten.
- Lambda MicroVM access in the chosen region and enough image/VM capacity.
  Ordinary Lambda function access is not sufficient.
- An enabled Bedrock model/inference profile with working inference access.
  For cross-region profiles, allow the profile and every destination model ARN.
  `aws bedrock get-inference-profile --inference-profile-identifier ...` lists them.
- Permission to create the resources above, pass their scoped roles and build a
  MicroVM image. The operator also needs scoped SSM Run Command, S3 deployment
  uploads, and Secrets Manager access to this installation's secret.
- A dedicated Slack app for testing. Its OAuth credentials, signing secret,
  allowed user IDs/team and approved email domains are supplied separately.

The default application digest contains PR #675 (source `9de19f6`). The gateway
digest is pinned rather than following a moving tag. Select a matching application
image and Docker bundle when changing releases. Neither secret values nor an
AWS access key belong in `.tfvars` or Terraform backend configuration.

For a custom build, push it to the `application_repository` Terraform output and
set `moyai_image` to that repository's immutable digest before installing. Build
with `--build-arg MOYAI_BUILD_SHA=$(git rev-parse HEAD)` and `--platform linux/amd64`
for the default host. The host can pull only that ECR repository; it has no push
permissions. Public pinned images remain supported. Apply the changed input
before running the installer. An existing installation still needs maintenance.

## 1. Provision the infrastructure

Run from the repository root:

```sh
uv sync --frozen
cp deploy/aws/terraform.tfvars.example deploy/aws/terraform.tfvars
# Edit hostname, zone, account-specific Bedrock ARNs and resource prefix.
export AWS_PROFILE=your-aws-profile
terraform -chdir=deploy/aws init
terraform -chdir=deploy/aws plan -out=installation.tfplan
terraform -chdir=deploy/aws apply installation.tfplan
```

Use your organization's encrypted, access-controlled remote Terraform backend
with locking for a retained installation. The module deliberately does not create
or select an organization's state backend. Protect local state/plan files until
migrating them. `.gitignore` excludes state, plans and private variable files.
Keep the provider lock file committed.

The host has no public IP or SSH ingress. Its only incoming application traffic
comes from the ALB; RDS only accepts the host security group. IMDSv2 is required.
SSM Session Manager is the maintenance access path. Docker starts only after the
dedicated encrypted volume is mounted. EC2 replacement does not delete that
independently managed volume.

## 2. Initialize credentials outside Terraform

Create a mode-0600 JSON file **outside the checkout** containing your Slack
settings. Set all six fields together, or use `{}` to start without Slack:

```json
{
  "SLACK_CLIENT_ID": "your-client-id",
  "SLACK_CLIENT_SECRET": "your-client-secret",
  "SLACK_SIGNING_SECRET": "your-signing-secret",
  "SLACK_TEAM_ID": "T_YOUR_WORKSPACE",
  "SLACK_SESSION_USERS": "U_YOUR_TEST_USER",
  "GOOGLE_ALLOWED_DOMAINS": "example.com"
}
```

Despite its name, `GOOGLE_ALLOWED_DOMAINS` also controls Slack identity eligibility.
It does not require Google OAuth. An empty domain policy rejects Slack users.

```sh
uv run python deploy/aws/quickstart.py secrets --secret-file /secure/path/moyai.json
```

The helper generates independent database, gateway and administrator passwords
and stores them in the empty Secrets Manager secret. It never prints them and
refuses to overwrite an existing value with different inputs. You can supply
`WORKSPACE_PASSWORD` explicitly (at least 24 characters). Retrieve that field from
the secret in Secrets Manager to sign in; do not paste its value into logs.
Remove the input file when your secret-management workflow no longer needs it.

RDS manages its separate bootstrap administrator password. The installer uses it
to create the `moyai` database login, then supplies only that login to the app.
RDS administrator rotation does not change the app's password. The trusted host
role can read both scoped secrets for installation/recovery; review this access
as part of your organization's production IAM policy. The runtime secret is not
automatically rotated. Rotate application credentials through deliberate
maintenance, not by changing the database password behind running services.

## 3. Build the guest and install

```sh
uv run python deploy/aws/quickstart.py image
uv run python deploy/aws/quickstart.py install
uv run python deploy/aws/quickstart.py status
```

The image helper uses Moyai's canonical ARM64/8-GiB guest packager and AWS build
API, including the readiness budget and native suspend/resume hooks. The pinned
Terraform AWS provider's image resource does not expose all these settings, so
this bounded build step uses the SDK. Its checksum/version receipt is stored in
S3; retries observe the same build. It will not replace a different pinned image
or silently upgrade checkpoint image versions. The guest image is **not managed
by Terraform destroy**; see teardown below.

Installation uploads the allowlisted Docker bundle and a checksummed bootstrap
script to private S3, then invokes SSM. The SSM request contains identifiers and
hashes, never secrets. The host fetches credentials itself and stores private
configuration on encrypted EBS. The gateway and all Moyai roles use the host's
workload identity. The installer uses the ordinary `moyai.sh configure`, `up`
and `check` paths, including schema initialization and split-role activation.

`status` reports the SSM command's completion separately from application E2E.
Open the Terraform `app_url` output, sign in with the administrator password, and
complete the Slack setup below. Infrastructure creation alone does not mean the
app, model, Slack or workspace recovery has been verified.

Identical installation retries retain existing credentials and installation keys.
If interruption occurs after configuration was published, the matching pending
receipt permits finishing startup without regenerating configuration. Different
destinations, images or settings are refused; use maintenance for changes.
If a step fails, inspect its SSM invocation, `/var/log/cloud-init-output.log`,
`/opt/moyai-aws/last-error.log` and the named service's CloudWatch logs privately.
Never attach raw secret-bearing host configuration to a support issue.

## 4. Connect Slack and verify both recovery paths

Register these URLs in the dedicated Slack app, using your `app_url`:

- OAuth redirect: `/oauth/slack/callback`
- Events: `/hooks/slack/events`
- Interactions: `/hooks/slack/interactions`

Enable the message/app-mention subscriptions and scopes requested by Moyai's
**Connections → Slack** flow, and complete OAuth there. A pasted bot token does
not replace that installation. Keep the sender/team/domain allowlist scoped while
testing. The Slack webhook must be reachable over HTTPS with its signing-secret
validation; do not put an interactive employee SSO challenge in front of it.

Use the actual Slack bot with the real Bedrock model:

1. Ask it to create a unique marker file, an executable verification script, a
   symlink and a deleted-file marker; install a small package. Ask it to start a
   background process with a random value held only in memory. Save the file
   through `save_artifact` and verify Slack delivers it in the same thread.
2. Let the completed session become `SUSPENDED` in AWS. Reply in the same thread
   asking it to read the existing values, without recreating them. Verify the
   same VM ID, PID and in-memory value. Run an active task longer than the idle
   window to verify keepalive.
3. Wait for actual maximum-lifetime termination, then reply again. Verify a new
   VM restores the exact file checksum, executable mode, symlink, deletion and
   installed package from S3. The old in-memory process should be absent.
4. Restart the trusted app while keeping RDS, S3 and the state volume. Verify
   login, history and another reply in that same Slack session.
5. Verify anonymous application requests and unsigned Slack events are rejected.

For a disposable acceptance deployment, set lifetime/idle to `600`/`60` **before
installation**. This exercises actual AWS expiry, not an eight-hour soak. Restore
the desired production settings through maintenance before retaining it.
See the [detailed recovery acceptance guide](../../docs/aws-lambda-microvms.md#slack-and-checkpointrestore-acceptance).

## Operations, upgrades and teardown

- Daily EBS recovery points are **crash-consistent**, not a demonstrated cross-service
  restore. RDS has seven-day backups and S3 has versioning. Keep a coordinated
  maintenance backup of the database, S3 references and installation keys before
  upgrades; perform a restore drill before depending on this for production.
- Protect `/srv/moyai`, including Docker volumes and the private installation
  keys. A restored database without its original encryption keys is insufficient.
- Use the [Docker maintenance/upgrade procedure](../docker/README.md), keeping
  `compose.aws.json`, the RDS CA mount and the private model network. Terraform
  changes do not upgrade a running application or rewrite its private `.env`.
  A new guest image only affects new computers; retain old images for checkpoints.
- Review the plan before changes that replace EC2, change subnets or detach EBS.
  Do not detach a live state volume or run two hosts against the same installation.
- EC2, RDS, NAT, ALB, storage, image builds and inference incur charges. Suspending
  guest computers does not stop the continuously running application services.
- To retire an installation, stop admission and drain using the maintenance
  procedure, export required data, and terminate only its guest VMs. Delete its
  guest image through the AWS MicroVM API only after no retained checkpoint needs
  it. The image ARN is in the Terraform `quickstart` output and S3 build receipt.
- Set `deletion_protection=false` and apply that change before `terraform destroy`.
  RDS takes a final snapshot. Nonempty/versioned S3 buckets, ECR repositories and backup vaults
  intentionally prevent automatic deletion; archive or explicitly dispose of
  their contents before retrying. Secrets have a 30-day recovery window. Destroy
  deletes the managed EBS volume, so verify your backups first. A later teardown
  with the same name needs a distinct final-snapshot identifier if one already exists.

## Local checks

```sh
terraform -chdir=deploy/aws fmt -check -recursive
terraform -chdir=deploy/aws validate
terraform -chdir=deploy/aws test
uv run pytest -q deploy/aws/tests/test_quickstart.py
```

Terraform's mocked provider tests and installer unit tests cover infrastructure
boundaries and retry behavior. They do not substitute for a real deployment,
model execution, Slack OAuth/delivery, or recovery acceptance.
