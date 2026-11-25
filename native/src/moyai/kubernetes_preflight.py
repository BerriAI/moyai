"""Read-only installation checks for the workspace control plane."""

import asyncio

import httpx

from .deployment_config import InstallationError
from .settings import Settings


def validate_template(pool: dict, template: dict, expected: str) -> None:
    if pool.get("spec", {}).get("sandboxTemplateRef", {}).get("name") != expected:
        raise InstallationError("The workspace warm pool must reference the configured template.")
    if pool.get("spec", {}).get("replicas") != 0:
        raise InstallationError("Set workspace warm pool replicas to zero for per-claim runtime credentials.")
    spec = template.get("spec", {})
    if spec.get("envVarsInjectionPolicy") != "Allowed":
        raise InstallationError("Use Allowed environment injection for the per-claim runtime digest.")
    pod = spec.get("podTemplate", {}).get("spec", {})
    if pod.get("automountServiceAccountToken") is not False:
        raise InstallationError("The workspace template must disable service account token automount.")
    if any(pod.get(field) for field in ("hostNetwork", "hostPID", "hostIPC")):
        raise InstallationError("Workspace pods must use their own network and process namespaces.")
    if spec.get("networkPolicyManagement") != "Unmanaged":
        raise InstallationError("Use Unmanaged template network policy with explicit workspace network isolation.")
    # A direct PVC reference would share data between sessions. The controller
    # gives each sandbox its own volume when it instantiates the claim template.
    claims = {
        item.get("metadata", {}).get("name") for item in spec.get("volumeClaimTemplates", [])
        if item.get("spec", {}).get("accessModes") in (["ReadWriteOnce"], ["ReadWriteOncePod"])
        and not item.get("spec", {}).get("volumeName")
    }
    containers = pod.get("containers", [])
    runtime = [
        container for container in containers
        if any(port.get("containerPort") == 9090 for port in container.get("ports", []))
    ]
    if len(runtime) != 1 or runtime[0] is not containers[0]:
        raise InstallationError("The first workspace container must serve the runtime on TCP 9090.")
    digest_arg = "--auth-token-sha256=$(WORKSPACE_TOKEN_DIGEST)"
    if (
        digest_arg not in runtime[0].get("args", [])
        or runtime[0].get("envFrom")
        or any(item.get("name") == "WORKSPACE_TOKEN_DIGEST" for item in runtime[0].get("env", []))
    ):
        raise InstallationError("Configure the runtime to require the digest supplied with each workspace claim.")
    if not any(
        mount.get("name") in claims and mount.get("mountPath") == "/workspace"
        and not mount.get("readOnly") and not mount.get("subPath") and not mount.get("subPathExpr")
        for mount in runtime[0].get("volumeMounts", [])
    ):
        raise InstallationError("Mount a per-sandbox writable claim template at /workspace on the runtime container.")
    if any(
        any(key in volume for key in ("secret", "hostPath", "persistentVolumeClaim", "csi"))
        or any("serviceAccountToken" in source or "secret" in source
               for source in volume.get("projected", {}).get("sources", []))
        for volume in pod.get("volumes", [])
    ):
        raise InstallationError("The workspace template includes a credential, host or external volume.")
    if any(
        any("secretRef" in item for item in container.get("envFrom", []))
        or any("secretKeyRef" in item.get("valueFrom", {}) for item in container.get("env", []))
        for container in [*containers, *pod.get("initContainers", [])]
    ):
        raise InstallationError("Workspace containers must not receive application secrets.")


async def check_workspace(configured: Settings) -> None:
    from k8s_agent_sandbox.constants import CLAIM_API_GROUP, CLAIM_API_VERSION, SANDBOX_API_GROUP
    from kubernetes_asyncio import client, config
    from kubernetes_asyncio.client.exceptions import ApiException
    from kubernetes_asyncio.config.config_exception import ConfigException

    # Verify the isolated process client's generated stubs before claiming any workspace.
    smoke = await asyncio.create_subprocess_exec(
        configured.sandbox_rpc_python, "-I", "-c",
        "from k8s_agent_sandbox.commands._process_stubs import process_pb2, process_pb2_grpc; "
        "assert hasattr(process_pb2, 'ExecuteWithLimitsRequest')",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(10):
            code = await smoke.wait()
    finally:
        if smoke.returncode is None:
            smoke.kill()
            await smoke.wait()
    if code:
        raise InstallationError("The workspace RPC interpreter cannot load the pinned process client.")
    namespace = configured.sandbox_namespace
    try:
        cluster = client.Configuration()
        config.load_incluster_config(client_configuration=cluster)
        async with client.ApiClient(cluster) as connection:
            connection.user_agent = "workspace-preflight"
            resources = client.CustomObjectsApi(connection)
            scope = (CLAIM_API_GROUP, CLAIM_API_VERSION, namespace)
            pool = await resources.get_namespaced_custom_object(*scope, "sandboxwarmpools", configured.sandbox_warm_pool)
            template = await resources.get_namespaced_custom_object(*scope, "sandboxtemplates", configured.sandbox_template)
            validate_template(pool, template, configured.sandbox_template)
            auth = client.AuthorizationV1Api(connection)
            for group, resource, verbs in (
                (CLAIM_API_GROUP, "sandboxclaims", ("get", "create", "watch", "patch", "delete")),
                ("", "persistentvolumeclaims", ("get",)),
                (SANDBOX_API_GROUP, "sandboxes", ("get",)),
            ):
                for verb in verbs:
                    attributes = client.V1ResourceAttributes(
                        namespace=namespace, group=group, resource=resource, verb=verb,
                    )
                    review = await auth.create_self_subject_access_review(
                        body=client.V1SelfSubjectAccessReview(
                            api_version="authorization.k8s.io/v1", kind="SelfSubjectAccessReview",
                            spec=client.V1SelfSubjectAccessReviewSpec(resource_attributes=attributes),
                        ),
                    )
                    if not review.status.allowed:
                        raise InstallationError(f"Workspace access requires {verb} on {resource} in the workspace namespace.")
    except (ApiException, ConfigException):
        raise InstallationError("Cannot verify the workspace pool, template or access using the in-cluster identity.") from None
    # This endpoint is public to the router's private network. A healthy router
    # and readable credential do not establish execution or per-claim authorization.
    configured.read_sandbox_router_token()
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, headers={"User-Agent": "workspace-preflight"}) as http:
        response = await http.get(configured.sandbox_router_url.rstrip("/") + "/healthz")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("status") != "ok":
            raise InstallationError("The workspace router did not report healthy.")
