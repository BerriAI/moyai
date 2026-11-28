"""Persistent, namespace-scoped coding workspaces."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import (
    CommandResult,
    WorkspaceCommand,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from pydantic_ai.workspaces.protocol import validate_timeout
from temporalio import activity

from .background_stop import finish
from .contracts import RunScope

if TYPE_CHECKING:
    from .settings import Settings

WORKDIR = "/workspace"
OWNER = "workspace-installation"
NONCE = "workspace-instance"
VOLUME_UID = "workspace-volume-uid"
TOKEN_ENV = "WORKSPACE_TOKEN_DIGEST"
TOKEN_FLAG = "--auth-token-sha256=$(WORKSPACE_TOKEN_DIGEST)"
NAME_PATTERN = re.compile(r"workspace-[0-9a-f]{40}")
STREAM_LIMIT = 16 * 1024 * 1024


class CommandOutcomeUnknown(WorkspaceError):
    """The command needs reconciliation before it may be repeated."""


def _hash(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()[:40]


def _claim_name(settings: Settings, scope: RunScope) -> str:
    return "workspace-" + _hash(settings.sandbox_installation_id, scope.session_id, scope.workspace_key)


def _identity(ref: WorkspaceRef) -> tuple[str, str]:
    name, _, uid = ref.id.partition(".")
    if ref.provider != "kubernetes" or not NAME_PATTERN.fullmatch(name):
        raise ValueError("Invalid Kubernetes workspace reference")
    try:
        if str(UUID(uid)) != uid:
            raise ValueError
    except ValueError:
        raise ValueError("Invalid Kubernetes workspace claim UID") from None
    return name, uid


@asynccontextmanager
async def _cluster():
    from k8s_agent_sandbox.async_k8s_helper import AsyncK8sHelper
    from kubernetes_asyncio import client, config

    configuration = client.Configuration()
    config.load_incluster_config(client_configuration=configuration)
    async with client.ApiClient(configuration) as api:
        api.user_agent = "workspace-control"
        yield AsyncK8sHelper(api_client=api), client.CustomObjectsApi(api)


class KubernetesSandbox(AbstractCapability[RunScope]):
    def __init__(self, settings: Settings):
        self.settings = settings

    def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None) -> KubernetesBackend | None:
        if ref is not None:
            return self.resume(ctx.deps, ref) if ref.provider == "kubernetes" else None
        return KubernetesBackend(self.settings, _claim_name(self.settings, ctx.deps))

    def backend(self, ref: WorkspaceRef) -> KubernetesBackend:
        name, _ = _identity(ref)
        return KubernetesBackend(self.settings, name, ref)

    def resume(self, scope: RunScope, ref: WorkspaceRef) -> KubernetesBackend:
        backend = self.backend(ref)
        if backend.name != _claim_name(self.settings, scope):
            raise PermissionError("Workspace reference belongs to a different session or installation")
        return backend

    async def destroy(self, ref: WorkspaceRef) -> None:
        backend = self.backend(ref)
        async with asyncio.timeout(30), _cluster() as (claims, _):
            claim = await claims.get_sandbox_claim(backend.name, backend.namespace)
            if claim is None:
                return
            uid, _ = backend.verify_claim(claim)
            await claims.delete_sandbox_claim(backend.name, backend.namespace, expected_uid=uid)


class KubernetesBackend:
    """The workspace library provides binary file operations through the command interface."""

    def __init__(self, settings: Settings, name: str, ref: WorkspaceRef | None = None):
        self.settings = settings
        self.name = name
        self.namespace = settings.sandbox_namespace
        self.owner = _hash(settings.sandbox_installation_id)
        self._ref = ref
        self._lock = asyncio.Lock()

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    def _token(self, nonce: str, pool: str) -> str:
        message = json.dumps(["workspace-access-v1", self.namespace, self.owner, self.name, pool, nonce])
        return hmac.digest(self.settings.master_key, message.encode(), "sha256").hex()

    @staticmethod
    def _env(token: str) -> dict[str, str]:
        return {"name": TOKEN_ENV, "value": hashlib.sha256(token.encode()).hexdigest()}

    def verify_claim(self, claim: dict[str, Any]) -> tuple[str, str]:
        metadata = claim.get("metadata", {})
        spec = claim.get("spec", {})
        uid = metadata.get("uid", "")
        nonce = metadata.get("annotations", {}).get(NONCE, "")
        pool = spec.get("warmPoolRef", {}).get("name", "")
        if (
            metadata.get("name") != self.name or metadata.get("namespace") != self.namespace
            or metadata.get("labels", {}).get(OWNER) != self.owner
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or not pool or (self._ref is None and pool != self.settings.sandbox_warm_pool)
        ):
            raise PermissionError("Workspace claim ownership could not be verified")
        token = self._token(nonce, pool)
        if spec.get("env") != [self._env(token)]:
            raise PermissionError("Workspace claim authentication does not match its owner")
        ref = WorkspaceRef(provider="kubernetes", id=f"{self.name}.{uid}")
        _identity(ref)
        if self._ref is not None and self._ref != ref:
            raise WorkspaceUnavailableError("The recorded workspace claim was replaced")
        return uid, token

    async def _claim(self, claims, objects) -> tuple[dict, str, str]:
        from k8s_agent_sandbox.constants import CLAIM_API_GROUP, CLAIM_API_VERSION, CLAIM_PLURAL_NAME

        async with asyncio.timeout(30):
            claim = await claims.get_sandbox_claim(self.name, self.namespace)
            if claim is None:
                if self._ref is not None:
                    raise WorkspaceUnavailableError("The recorded workspace claim is gone")
                nonce = secrets.token_hex(16)
                pool = self.settings.sandbox_warm_pool
                manifest = {
                    "apiVersion": f"{CLAIM_API_GROUP}/{CLAIM_API_VERSION}", "kind": "SandboxClaim",
                    "metadata": {
                        "name": self.name, "labels": {OWNER: self.owner},
                        "annotations": {NONCE: nonce, VOLUME_UID: "pending"},
                    },
                    "spec": {"warmPoolRef": {"name": pool}, "env": [self._env(self._token(nonce, pool))]},
                }
                try:
                    # Leave time for a GET when the server commits a POST but its reply is lost.
                    async with asyncio.timeout(15):
                        claim = await objects.create_namespaced_custom_object(
                            group=CLAIM_API_GROUP, version=CLAIM_API_VERSION, plural=CLAIM_PLURAL_NAME,
                            namespace=self.namespace, body=manifest,
                        )
                except Exception:
                    claim = await claims.get_sandbox_claim(self.name, self.namespace)
                    if claim is None:
                        raise
            uid, token = self.verify_claim(claim)
            self._ref = WorkspaceRef(provider="kubernetes", id=f"{self.name}.{uid}")
            if claim["metadata"].get("deletionTimestamp"):
                raise WorkspaceUnavailableError("The workspace claim is terminating")
            return claim, uid, token

    async def _verify_volume(self, claims, objects, claim: dict, sandbox: dict, volume_name: str) -> None:
        from k8s_agent_sandbox.constants import (
            CLAIM_API_GROUP,
            CLAIM_API_VERSION,
            CLAIM_PLURAL_NAME,
            SANDBOX_API_GROUP,
            SANDBOX_API_VERSION,
        )
        from kubernetes_asyncio.client.exceptions import ApiException

        metadata = sandbox["metadata"]
        pvc_name = f"{volume_name}-{metadata['name']}"
        try:
            pvc = await claims.core_v1_api.read_namespaced_persistent_volume_claim(pvc_name, self.namespace)
        except ApiException as error:
            if error.status == 404:
                raise WorkspaceUnavailableError("The recorded workspace volume is gone") from None
            raise
        owner_version = f"{SANDBOX_API_GROUP}/{SANDBOX_API_VERSION}"
        if (
            not pvc.metadata or not pvc.metadata.uid or pvc.metadata.deletion_timestamp
            or pvc.metadata.name != pvc_name or pvc.metadata.namespace != self.namespace
            or not pvc.status or pvc.status.phase != "Bound" or not pvc.spec or not pvc.spec.volume_name
            or not any(
                owner.api_version == owner_version and owner.kind == "Sandbox" and owner.controller
                and owner.uid == metadata["uid"] and owner.name == metadata["name"]
                for owner in pvc.metadata.owner_references or ()
            )
        ):
            raise WorkspaceUnavailableError("The workspace volume is unbound or owned by another resource")
        # Only the first acquisition may bind storage. Subsequent workers compare
        # its immutable UID even if a controller recreates a PVC with the same name.
        # A resourceVersion precondition prevents concurrent acquisition overwrites.
        for _ in range(4):
            uid, _token = self.verify_claim(claim)
            meta = claim["metadata"]
            recorded = meta["annotations"].get(VOLUME_UID)
            if meta.get("deletionTimestamp") or not meta.get("resourceVersion"):
                raise WorkspaceUnavailableError("The workspace claim is terminating or unavailable")
            if recorded == pvc.metadata.uid:
                return
            if recorded != "pending":
                raise WorkspaceUnavailableError("The recorded workspace volume was replaced or is unavailable")
            try:
                claim = await objects.patch_namespaced_custom_object(
                    group=CLAIM_API_GROUP, version=CLAIM_API_VERSION, namespace=self.namespace,
                    plural=CLAIM_PLURAL_NAME, name=self.name, _content_type="application/merge-patch+json",
                    body={"metadata": {"uid": uid, "resourceVersion": meta["resourceVersion"],
                                       "annotations": {VOLUME_UID: pvc.metadata.uid}}},
                )
            except Exception:
                # Reconcile a conflicting write or a committed patch with a lost reply.
                claim = await claims.get_sandbox_claim(self.name, self.namespace)
                if claim is None or claim["metadata"].get("resourceVersion") == meta["resourceVersion"]:
                    raise
        self.verify_claim(claim)
        if claim["metadata"].get("deletionTimestamp") or claim["metadata"]["annotations"].get(VOLUME_UID) != pvc.metadata.uid:
            raise WorkspaceUnavailableError("Could not verify the workspace volume binding")

    async def _target(self) -> tuple[str, str, str]:
        from k8s_agent_sandbox.constants import CLAIM_API_GROUP, CLAIM_API_VERSION

        self.settings.validate_kubernetes_workspace()
        async with _cluster() as (claims, objects):
            async with self._lock:
                # Record the UID even if cancellation races a successful creation.
                claim, uid, token = await finish(asyncio.create_task(self._claim(claims, objects)))
            async with asyncio.timeout(185):
                name = await claims.wait_for_claim_ready(
                    self.name, self.namespace, timeout=180, expected_uid=uid, initial_claim=claim,
                    resource_version=claim["metadata"].get("resourceVersion"),
                )
                sandbox = await claims.get_sandbox(name, self.namespace)
            if not sandbox:
                raise WorkspaceUnavailableError("The bound workspace is gone")
            meta = sandbox.get("metadata", {})
            if (
                meta.get("name") != name or meta.get("namespace") != self.namespace
                or not meta.get("uid") or meta.get("deletionTimestamp") or not any(
                    owner.get("kind") == "SandboxClaim" and owner.get("uid") == uid
                    and owner.get("apiVersion") == f"{CLAIM_API_GROUP}/{CLAIM_API_VERSION}"
                    and owner.get("name") == self.name and owner.get("controller") is True
                    for owner in meta.get("ownerReferences", [])
                )
            ):
                raise PermissionError("The bound workspace does not have the verified claim as its owner")
            spec = sandbox.get("spec", {})
            pod = spec.get("podTemplate", {}).get("spec", {})
            containers = pod.get("containers", [])
            runtime = containers[0] if containers else {}
            volumes = {
                item.get("metadata", {}).get("name") for item in spec.get("volumeClaimTemplates", [])
                if item.get("spec", {}).get("accessModes") in (["ReadWriteOnce"], ["ReadWriteOncePod"])
                and not item.get("spec", {}).get("volumeName")
            }
            mounted = [
                mount["name"] for mount in runtime.get("volumeMounts", []) if mount.get("name")
                and mount["name"] in volumes and mount.get("mountPath") == WORKDIR
                and not any(mount.get(key) for key in ("readOnly", "subPath", "subPathExpr"))
            ]
            if (
                pod.get("automountServiceAccountToken") is not False or len(mounted) != 1
                or TOKEN_FLAG not in runtime.get("args", []) or self._env(token) not in runtime.get("env", [])
            ):
                raise WorkspaceError("Workspace template requires runtime authentication and a private workspace volume")
            async with asyncio.timeout(30):
                await self._verify_volume(claims, objects, claim, sandbox, mounted[0])
            return name, meta["uid"], token

    async def working_dir(self) -> str:
        await self._target()
        return WORKDIR

    async def run(
        self, command: WorkspaceCommand, *, shell: bool = False,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
    ) -> CommandResult:
        validate_timeout(timeout)
        if isinstance(command, str) != shell:
            raise TypeError("String commands require shell=True and argument sequences require shell=False")
        argv = ["/bin/sh", "-c", command] if isinstance(command, str) else list(command)
        if not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise ValueError("Command requires a nonempty sequence of valid string arguments")
        environment = dict(env or {})
        if any(not k or "=" in k or "\x00" in k or "\x00" in v for k, v in environment.items()):
            raise ValueError("Invalid command environment")
        if activity.in_activity() and activity.info().attempt > 1:
            raise CommandOutcomeUnknown("An earlier activity delivery may have run this command; reconcile its outcome")
        name, uid, token = await self._target()
        url = urlsplit(self.settings.sandbox_router_url)
        auth = self.settings.read_sandbox_router_token()
        # Use the configured authority literally, including the explicit port for IPv6.
        target = url.netloc if url.port else f"{url.netloc}:443"
        result = await _execute(self.settings.sandbox_rpc_python, {
            "target": target, "name": name, "uid": uid, "namespace": self.namespace,
            "router_token": auth, "runtime_token": token, "argv": argv, "env": environment,
            "timeout": timeout, "limit": STREAM_LIMIT,
        })
        output = {"stdout": result["stdout"], "stderr": result["stderr"]}
        if result["outcome"] == "timeout":
            raise WorkspaceTimeoutError("Workspace command timed out", **output)
        if result["outcome"] == "truncated":
            raise WorkspaceOutputLimitError("Workspace command exceeded the output limit", limit=STREAM_LIMIT, **output)
        return CommandResult(exit_code=result["exit_code"], **output)


async def _execute(interpreter: str, request: dict[str, Any]) -> dict[str, Any]:
    # The two SDKs require incompatible protobuf major versions. Keep the helper
    # in its own environment; credentials and user arguments travel only on stdin.
    data = json.dumps({**request, "worker_pid": os.getpid()}).encode()
    process = await asyncio.create_subprocess_exec(
        interpreter, "-I", str(Path(__file__).with_name("workspace_rpc.py")),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={k: os.environ[k] for k in ("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", "SSL_CERT_FILE") if k in os.environ},
    )
    try:
        raw, _ = await process.communicate(data)
    except BaseException:
        # Closing the gRPC transport cancels ExecuteWithLimits at sandboxd,
        # which kills the foreground process group. Never send a second command.
        with suppress(ProcessLookupError):
            process.kill()
        await finish(asyncio.create_task(process.wait()))
        raise
    if process.returncode == 0:
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        if (
            result.get("outcome") in {"exited", "truncated", "timeout"}
            and isinstance(result.get("exit_code"), int)
            and all(isinstance(result.get(key), str) for key in ("stdout", "stderr"))
        ):
            return result
        if result.get("outcome") == "refused":
            raise PermissionError("The runtime or router refused this workspace command")
        if result.get("outcome") == "invalid":
            raise WorkspaceError("The runtime rejected the command or its execution limits")
    raise CommandOutcomeUnknown("The workspace command outcome is indeterminate; reconcile before repeating it")
