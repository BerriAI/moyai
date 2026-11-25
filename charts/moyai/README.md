# Moyai Helm chart

Install a **fresh** split-role Moyai instance at an HTTPS hostname root or under
a path such as https://gateway.example.com/moyai. API, coordinator, broker and
worker use the same immutable application image in one pod. A small proxy routes
requests; the image owns startup, role identity and activation logic.

This is a single-instance deployment, not an HA chart. Do not scale the pod:
private installation state and canonical role ownership must remain exclusive.
Bundled PostgreSQL/Temporal are persistent development dependencies. Use external
services when availability requirements exceed this layout. No Terraform, cloud
resources, DNS or Cloudflare policies are created.

## Prerequisites and secrets

- Kubernetes 1.29+, Helm 3.19+ and an existing namespace.
- A private S3-compatible bucket and workload credentials or storage keys.
- A reachable LiteLLM Responses endpoint and credentials for the selected workspace provider.
- PostgreSQL and Temporal, or the bundled dev dependencies below.
- An ingress/tunnel preserving Host, streaming and WebSockets, with a public
  broker URL that reaches this pod before activation.
- A suitable persistent-volume StorageClass. Use a retained/encrypted class on
  EKS. The chart creates namespaced PVCs, not a cluster-wide StorageClass.

Create secrets through your secret manager/ExternalSecrets **before** installing.
Keep credentials out of Helm values and Git. The Secret named by runtimeSecret
contains OBJECT_STORAGE_BUCKET, OBJECT_STORAGE_REGION, LITELLM_API_BASE,
LITELLM_API_KEY and AGENT_MODEL. The default Modal provider also uses MODAL_TOKEN_ID,
MODAL_TOKEN_SECRET and MODAL_APP_NAME.
Optional keys include storage credentials, TEMPORAL_API_KEY, WORKSPACE_PASSWORD
and Cloudflare settings from [the Docker installer](../../deploy/docker/README.md).
Missing signing/encryption keys are generated once into the private state PVC.

The Secret named by databaseSecret must contain MOYAI_DATABASE_URL. With bundled
PostgreSQL, also supply POSTGRES_PASSWORD, MOYAI_DB_PASSWORD and TEMPORAL_PASSWORD.
The application URL must use user/database moyai, the same MOYAI_DB_PASSWORD
(URL-encoded), and host <release>-moyai-postgres. For release product-dev, use
product-dev-moyai-postgres. Each installation ID must be unique for shared
databases, buckets and queues.

## Install

After a Moyai release publishes this chart:

    helm install product-dev oci://ghcr.io/berriai/charts/moyai \
      --version 0.2.0 --namespace moyai-product-dev -f product-dev.yaml

A release chart pins the image digest built from the same revision. For a source
checkout, build/push its Docker image and explicitly set image.digest. An older
image lacks the chart runtime and path support. The fake digests in ci/*.yaml are
only for rendering, never deployment.

Example product-dev.yaml:

    installationId: moyaidev
    publicUrl: https://gateway.example.com/moyai
    runtimeSecret: moyai-runtime
    databaseSecret: moyai-database
    persistence:
      storageClass: retained-encrypted-ebs
    postgresql:
      enabled: true
      persistence:
        storageClass: retained-encrypted-ebs
    temporal:
      bundled: true
      namespace: moyai-product-dev
    config:
      OBJECT_STORAGE_PREFIX: product-dev/product
      MOYAI_NATIVE_OBJECT_PREFIX: product-dev/native
    serviceAccount:
      name: moyai

For external services, leave bundled flags false and set temporal.address,
temporal.namespace and temporal.tls. Set the database URL in the existing Secret.
Use persistence.existingClaim to adopt an existing state claim; an existing service
account needs serviceAccount.create=false and serviceAccount.name.

## Kubernetes workspaces

The opt-in provider uses the SIG sandbox controller and extensions v1.0.12, the
Python SDK 1.0.12 and sandboxd built from tag v1.0.12. Install the controller,
reviewed template, warm pool and private router before the application. These
can be separately pinned releases. The application worker uses a separate
interpreter for the process RPC client, supplied in the matching release image.

    installationId: dev01
    kubernetesWorkspaces:
      enabled: true
      namespace: session-workspaces
      warmPool: session-pool
      template: session-runtime
      routerUrl: https://router.routing.svc.cluster.local
      routerNamespace: routing
      routerPodLabels: {app: sandbox-router}
      routerAudience: workspace-router

Enabling this block makes Kubernetes the default for new workspaces. To change
that default later, set `config.SANDBOX_PROVIDER` to the new provider and
supply its credentials. Keep the block enabled while existing sessions retain
Kubernetes workspace references.

Allocate one dedicated workspace namespace and one least-privilege application
service account per installation. Install the workspace namespace with Pod
Security Admission enforcing the `restricted` profile and a CNI that implements
NetworkPolicy. Keep credentials, application pods and router pods outside this
namespace. Restrict who can create or change templates, claims, pools, pods and
network policies there.

The chart creates a Role and RoleBinding in the workspace namespace. It grants
get/create/watch/patch/delete on sandbox claims, get on sandboxes and volume
claims, and get on the named pool and template. The worker records the bound
volume UID on its owning claim so recovery can detect a replaced volume. The chart mounts
an API credential and a separate short-lived router credential only into the
initializer and worker. The kubelet rotates both. The initializer reads the
configured template and pool, checks claim permissions, loads the isolated RPC
client and calls the private router health endpoint. A successful preflight
does not exercise a process RPC. Run a real session before promoting an install.

With `application.mode: externalFleets`, the chart manages the workspace
RoleBinding but the operator supplies the role pods. Configure the external
worker and installation initializer with the bound service account and the
same projected API and router credentials as the single-pod template. Keep
those token mounts out of other roles.

Configure an operator-owned router with TLS and HTTP/2, a certificate trusted by
the application and RPC interpreter, and TokenReview authentication with
`--authz-tokenreview-require-token=true` and
`--authz-tokenreview-audiences=workspace-router`. Grant the router its own
TokenReview and discovery permissions. Limit router ingress to the trusted
application pod or a dedicated authenticated proxy. TokenReview establishes the
caller identity. The worker presents a separate per-claim credential to the
runtime. Restrict router egress to DNS, the Kubernetes API and the workspace
runtime port. Configure the authenticated broker endpoint separately using the
private broker instructions below. Inference credentials belong to the application.

The chart's workspace NetworkPolicy denies egress and accepts TCP 9090 solely
from the selected router pods. This policy covers **every pod** in the specified
workspace namespace. Kubernetes combines allowed traffic across NetworkPolicies.
Set the template's `networkPolicyManagement: Unmanaged` and check for overlapping
policies before permitting untrusted workloads.

The [example pool and template](examples/workspace-pool.yaml) use
`extensions.agents.x-k8s.io/v1beta1`. Replace the placeholder image digest and
select a suitable StorageClass before installing them.

- Set `spec.envVarsInjectionPolicy: Allowed` and `spec.volumeClaimTemplatesPolicy: Disallowed`.
- Set `spec.podTemplate.spec.automountServiceAccountToken: false`. Use a
  dedicated unprivileged pod identity and omit secret mounts and environment
  imports. Use an unprivileged, digest-pinned sandboxd image containing a shell
  and Python 3. Apply CPU, memory and storage limits.
- Make the first container serve sandboxd on TCP 9090 with working directory
  and root directory `/workspace`. Include
  `--auth-token-sha256=$(WORKSPACE_TOKEN_DIGEST)` in its container arguments.
  Leave the digest environment variable undefined in the template. Each claim
  supplies its own digest. The bearer credential stays with the worker.
- Mount a writable volume at `/workspace` by its `spec.volumeClaimTemplates`
  name. Use a dynamically provisioned ReadWriteOnce or ReadWriteOncePod volume.
  A session keeps its files when the worker or runtime pod is replaced.
- Set the pool's `spec.sandboxTemplateRef.name` to the configured template and
  `spec.replicas: 0`. The per-claim digest requires a fresh sandbox for every
  allocation. Keep the namespace, pool, installation identity and encryption
  key stable for existing workspaces.

This provider supports shell commands, workspace files and reconnecting to
the original volume. Deleting a conversation in the UI is reversible and
retains its claim, pod and storage. Budget resources for retained conversations.
Releasing a claim ends that workspace and triggers the controller's cleanup.
The StorageClass reclaim policy determines what happens to the underlying disk.

The native UI can start a coding session without a repository selection.
Desktop and browser controls, credential-bearing commands, automatic repository
imports, prepared environments, and machine snapshots are unavailable with this
provider. Finish active sessions before a release that requires a maintenance
snapshot. An unresolved command receipt requires reconciliation before resuming.

Build the coding image from the same source revision as the worker. The supplied
Dockerfile includes the runtime, shell, Python, Git, Node, npm and the bundled
skills at the paths used by the worker. From the repository root run
`docker build -f deploy/docker/Dockerfile.workspace -t workspace-runtime:local .`.
Use the resulting image digest in the workspace template.

For a disposable installation, run `native/tests/kubernetes_session_probe.py`
inside the worker using its application interpreter. Pass `create <fresh-uuid>`
and save the two returned IDs. Replace the worker and the first workspace pod,
then run `verify <same-uuid> <first-id> <second-id>`. Release the two test
claims with `cleanup` and the same three arguments.

For a deployment outside Helm, supply the corresponding `SANDBOX_PROVIDER`,
`SANDBOX_NAMESPACE`, `SANDBOX_WARM_POOL`, `SANDBOX_TEMPLATE`,
`SANDBOX_ROUTER_URL` and `MOYAI_INSTALLATION_ID` settings to the native
installation. Set `SANDBOX_ROUTER_TOKEN_FILE` to a rotating token mount or use
a private `SANDBOX_ROUTER_TOKEN`. Run the initializer and execution worker
with in-cluster service account credentials. `SANDBOX_RPC_PYTHON` selects the
isolated interpreter when the installation uses a different filesystem layout.
Install it from the independent lock with
`uv sync --project native/workspace-rpc --locked --no-dev --no-install-project`.

## Ingress and Cloudflare

The Service <release>-moyai:8080 is ClusterIP. Add its route to the existing gateway
Ingress, or enable ingress.enabled and configure ingress.className, annotations
and TLS. The optional Ingress derives host and Prefix path from publicUrl.
ALB grouping depends on the installed controller: creating another Ingress does
not promise reuse of the existing ALB. For EKS Auto Mode, configure sharing through
your IngressClass/IngressClassParams or the existing gateway Ingress.

Forward /moyai and descendants **without rewriting**. The proxy selects roles
using the external path; the app normalizes it before authentication and routing.
/moyai redirects to /moyai/. The chart proxy rejects sibling routes. Root-host
deployments use the same image and chart. Probes use internal /health. Disable
buffering and allow long-lived streams/WebSockets in additional ingress layers.

The public /moyai/broker/health must reach the broker before activation. The
Service deliberately publishes not-ready endpoints to break that dependency.
The proxy returns 503 for browser/webhook routes until role identities match,
the public broker check succeeds and the persisted resume receipt is confirmed.
The proxy mounts only a nonsecret activation marker, never private state.

For Cloudflare Access, protect prefixed browser and broker routes with their
appropriate audiences and supply broker service credentials for activation.
Webhook exceptions remain exact, signed application routes, with the prefix
added in the external policy. /cdn-cgi/access/logout stays at the hostname root.
Register enabled OAuth callbacks with the prefix, for example
https://gateway.example.com/moyai/auth/google/callback.
A path is not a browser security boundary: sibling apps share an origin. Use
separate hostnames for applications that must not share that trust boundary.

## Restart, recovery and upgrades

Setup validates dependencies, creates empty installation schemas, starts the four
owners and activates the installation. Readiness verifies HTTP and release
identity; it does not prove model execution. Sign in, run a real file task, restart
the pod and verify the session/file remain available.

This chart supports fresh installation and restarts of the **same image**. It
does not implement an automatic Kubernetes image-upgrade controller. Changing
the digest fails closed against persisted state. Plan a reviewed maintenance
procedure using moyai.deployment begin-upgrade, stopped old owners, apply-upgrade
and resume, as described in the Docker maintenance guide. Operator jobs must
mount the same private state and use the selected source or target image.
Do not use Helm rollback to reverse schema migrations or delete state to make
an upgrade start. Automatic Argo image updates are inappropriate. Public URL/path
and data-destination changes also require explicit migration.

PVCs carry Helm keep and Argo prune/delete protection. Choose a Retain StorageClass
and back up installation state, PostgreSQL and objects together. Helm uninstall
retains claims, but does not make backups. Adopt retained claims deliberately
through existingClaim; never replace state belonging to populated schemas.

## Publication and validation

The Docker release workflow packages this chart with the just-published image
digest, pushes to oci://ghcr.io/berriai/charts, and attaches the archive to the same
GitHub release. Bump Chart.yaml's version for each publication; consumers pin that
version. On first publish an administrator may need to configure registry package
visibility and repository Actions access. PRs validate without publishing.

    helm lint charts/moyai -f charts/moyai/ci/bundled.yaml
    helm lint charts/moyai -f charts/moyai/ci/external.yaml
    uv run pytest charts/tests/test_chart.py --confcutdir=charts -q

## Private UI with a separate broker endpoint

Set `publicUrl` to the private browser URL and `brokerUrl` to the HTTPS machine
URL, using the same base path (both root URLs are supported). The app uses
`MOYAI_BROKER_URL` for native worker inference and activation's broker identity
check. Cookies, OAuth redirects and browser origin checks still use
`MOYAI_PUBLIC_URL`. Omitting `brokerUrl` retains existing routing.

When configured, `<release>-moyai-broker:8080` is a **ClusterIP** Service targeting
a dedicated proxy listener. Only `<basePath>/broker/*` reaches the broker role;
UI, login, hooks and all other paths return 404. It publishes unready endpoints
so activation can verify the route before admitting browser traffic. Point an
existing authenticated Cloudflare tunnel at this Service; no extra Ingress or
AWS load balancer is created. The chart does not create DNS or Access policies.

Protect the entire broker hostname with a separate Cloudflare Access application
and a Service Auth policy allowing only a dedicated service token. Enable Access
JWT validation in the tunnel. Supply the team domain, distinct UI/broker audiences,
and service token pair through `runtimeSecret`, never Helm values. The application
still verifies the broker JWT audience and signed, single-request inference
permission. Access credentials do not grant access to UI paths or authorize model
requests on their own. Do not add browser Allow or Bypass policies to the broker.

Changing only the broker transport preserves the persisted installation identity
and keys. It still requires the normal deployment restart/activation check. Use
an image built from the same revision as this chart; older images ignore the new
setting. Docker installs can also set `MOYAI_BROKER_URL` in `.env` and route only
broker paths through their externally managed authenticated proxy.
