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
- A reachable LiteLLM Responses-capable model and Modal credentials.
- PostgreSQL and Temporal, or the bundled dev dependencies below.
- An ingress/tunnel preserving Host, streaming and WebSockets, with a public
  broker URL that reaches this pod before activation.
- A suitable persistent-volume StorageClass. Use a retained/encrypted class on
  EKS. The chart creates namespaced PVCs, not a cluster-wide StorageClass.

Create secrets through your secret manager/ExternalSecrets **before** installing.
Keep credentials out of Helm values and Git. The Secret named by runtimeSecret
contains OBJECT_STORAGE_BUCKET, OBJECT_STORAGE_REGION, LITELLM_API_BASE,
LITELLM_API_KEY, AGENT_MODEL, MODAL_TOKEN_ID, MODAL_TOKEN_SECRET and MODAL_APP_NAME.
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
      --version 0.1.0 --namespace moyai-product-dev -f product-dev.yaml

A release chart pins the image digest built from the same revision. For a source
checkout, build/push its Docker image and explicitly set image.digest. An older
image lacks the chart runtime and path support. The fake digests in ci/*.yaml are
only for rendering, never deployment.

Example product-dev.yaml:

    installationId: moyaidev
    publicUrl: https://gateway.product-dev.internal.litellm.ai/moyai
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
