import asyncio
import copy
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic_ai.workspaces import (
    Workspace,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from moyai import kubernetes_workspace as provider
from moyai.contracts import RunScope
from moyai.settings import Settings


class Resources:
    def __init__(self):
        self.claims = {}
        self.creations = 0
        self.sandbox_uid = str(uuid4())
        self.volume_uids = {}
        self.core_v1_api = self
        self.patches = 0
        self.lose_patch_reply = False
        self.return_foreign_sandbox = False
        self.ephemeral = False
        self.delete = None
        self.lose_create_reply = False

    async def get_sandbox_claim(self, name, namespace):
        return copy.deepcopy(self.claims.get(name))

    async def create_namespaced_custom_object(self, *, namespace, body, **kwargs):
        name = body["metadata"]["name"]
        if name in self.claims:
            raise RuntimeError("already exists")
        self.creations += 1
        value = copy.deepcopy(body)
        value["metadata"].update(namespace=namespace, uid=str(uuid4()), resourceVersion="12")
        self.claims[name] = value
        if self.lose_create_reply:
            raise ConnectionError("response lost")
        return copy.deepcopy(value)

    async def patch_namespaced_custom_object(self, *, namespace, name, body, **kwargs):
        meta = self.claims[name]["metadata"]
        assert meta["uid"] == body["metadata"]["uid"]
        if meta["resourceVersion"] != body["metadata"]["resourceVersion"]:
            raise RuntimeError("conflict")
        meta["annotations"].update(body["metadata"]["annotations"])
        meta["resourceVersion"] = str(int(meta["resourceVersion"]) + 1)
        self.patches += 1
        if self.lose_patch_reply:
            raise ConnectionError("response lost")
        return copy.deepcopy(self.claims[name])

    async def read_namespaced_persistent_volume_claim(self, name, namespace):
        from k8s_agent_sandbox.constants import SANDBOX_API_GROUP, SANDBOX_API_VERSION

        owner = SimpleNamespace(
            controller=True, uid=self.sandbox_uid, name=name.removeprefix("workspace-"),
            kind="Sandbox", api_version=f"{SANDBOX_API_GROUP}/{SANDBOX_API_VERSION}",
        )
        meta = SimpleNamespace(
            name=name, namespace=namespace, deletion_timestamp=None, owner_references=[owner],
            uid=self.volume_uids.setdefault(name, str(uuid4())),
        )
        return SimpleNamespace(metadata=meta, status=SimpleNamespace(phase="Bound"),
                               spec=SimpleNamespace(volume_name="test-disk"))

    async def wait_for_claim_ready(self, name, namespace, *, expected_uid, initial_claim, **kwargs):
        assert self.claims[name]["metadata"]["uid"] == expected_uid
        return name

    async def get_sandbox(self, name, namespace):
        claim = self.claims[name]
        uid = "foreign" if self.return_foreign_sandbox else claim["metadata"]["uid"]
        return {
            "metadata": {"name": name, "namespace": namespace, "uid": self.sandbox_uid,
                         "ownerReferences": [{"kind": "SandboxClaim", "name": name, "uid": uid,
                                              "apiVersion": claim["apiVersion"], "controller": True}]},
            "spec": {
                "volumeClaimTemplates": [] if self.ephemeral else [
                    {"metadata": {"name": "workspace"}, "spec": {"accessModes": ["ReadWriteOnce"]}},
                ],
                "podTemplate": {"spec": {"automountServiceAccountToken": False, "containers": [{
                    "args": [provider.TOKEN_FLAG], "env": claim["spec"]["env"],
                    "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
                }]}},
            },
        }

    async def delete_sandbox_claim(self, name, namespace, *, expected_uid):
        assert self.claims[name]["metadata"]["uid"] == expected_uid
        self.delete = (name, expected_uid)
        del self.claims[name]


@pytest.fixture
def cluster(monkeypatch):
    resources = Resources()

    @asynccontextmanager
    async def connect():
        yield resources, resources

    monkeypatch.setattr(provider, "_cluster", connect)
    return resources


@pytest.fixture
def capability(tmp_path):
    return provider.KubernetesSandbox(Settings(
        _env_file=None, data_dir=tmp_path, sandbox_namespace="workspaces", sandbox_installation_id="test",
        sandbox_warm_pool="code-pool", sandbox_template="code-template",
        sandbox_router_url="https://router.example.test", sandbox_router_token="router-credential",
    ))


@pytest.fixture
def scope():
    return RunScope(session_id="first", actor_id="author", turn_id="turn", model="runtime")


def new(capability, scope):
    return capability.get_workspace(SimpleNamespace(deps=scope), ref=None)


async def test_acquire_reconnect_replace_pod_and_separate_sessions(cluster, capability, scope):
    first = new(capability, scope)
    retry = new(capability, replace(scope, turn_id="followup"))
    second = new(capability, replace(scope, session_id="second"))
    assert first.ref is None
    cluster.lose_create_reply = True
    cluster.lose_patch_reply = True
    assert await asyncio.gather(first.working_dir(), retry.working_dir(), second.working_dir()) == ["/workspace"] * 3
    assert first.ref == retry.ref and first.ref != second.ref
    assert cluster.creations == cluster.patches == 2
    restored = capability.resume(scope, first.ref)
    assert restored.ref == first.ref
    assert await restored.working_dir() == "/workspace"
    for other in (replace(scope, session_id="other"), replace(scope, actor_id="other")):
        with pytest.raises(PermissionError):
            capability.resume(other, first.ref)
    cluster.sandbox_uid = str(uuid4())
    assert (await restored._target())[1] == cluster.sandbox_uid
    assert cluster.creations == 2


async def test_replaced_claim_and_foreign_or_ephemeral_sandbox_fail_closed(cluster, capability, scope):
    backend = new(capability, scope)
    await backend.working_dir()
    cluster.return_foreign_sandbox = True
    with pytest.raises(PermissionError):
        await backend.working_dir()
    cluster.return_foreign_sandbox = False
    cluster.ephemeral = True
    with pytest.raises(WorkspaceError, match="volume"):
        await backend.working_dir()
    claim = cluster.claims[backend.name]
    claim["metadata"]["uid"] = str(uuid4())
    with pytest.raises(WorkspaceUnavailableError, match="replaced"):
        await capability.destroy(backend.ref)
    assert cluster.delete is None
    del cluster.claims[backend.name]
    with pytest.raises(WorkspaceUnavailableError, match="gone"):
        await backend.working_dir()
    assert cluster.creations == 1


async def test_refuses_recreated_volume_before_command_submission(cluster, capability, scope, monkeypatch):
    backend = new(capability, scope)
    await backend.working_dir()
    ref = backend.ref
    pvc_name, = cluster.volume_uids
    cluster.volume_uids[pvc_name] = str(uuid4())
    submit = AsyncMock()
    monkeypatch.setattr(provider, "_execute", submit)
    with pytest.raises(WorkspaceUnavailableError, match="volume was replaced"):
        await capability.resume(scope, ref).run(["touch", "effect"])
    submit.assert_not_awaited()
    assert cluster.patches == 1


async def test_owned_cleanup_requires_verified_digest_and_uid(cluster, capability, scope):
    backend = new(capability, scope)
    await backend.working_dir()
    ref = backend.ref
    metadata = cluster.claims[backend.name]["metadata"]
    original_nonce = metadata["annotations"][provider.NONCE]
    metadata["annotations"][provider.NONCE] = "1" * 32
    with pytest.raises(PermissionError, match="authentication"):
        await capability.destroy(ref)
    metadata["annotations"][provider.NONCE] = original_nonce
    await capability.destroy(ref)
    await capability.destroy(ref)
    assert cluster.delete == (backend.name, metadata["uid"])


async def test_two_workers_racing_to_bind_volume_use_one_conditional_patch(cluster, capability, scope, monkeypatch):
    first, retry = new(capability, scope), new(capability, scope)
    barrier = asyncio.Barrier(2)
    real_patch = cluster.patch_namespaced_custom_object
    versions = []

    async def concurrent_patch(**kwargs):
        versions.append(kwargs["body"]["metadata"]["resourceVersion"])
        await barrier.wait()
        return await real_patch(**kwargs)

    monkeypatch.setattr(cluster, "patch_namespaced_custom_object", concurrent_patch)
    result = await asyncio.wait_for(asyncio.gather(first.working_dir(), retry.working_dir()), timeout=10)
    assert result == ["/workspace", "/workspace"]
    assert len(versions) == 2 and len(set(versions)) == 1
    assert first.ref == retry.ref and cluster.patches == cluster.creations == 1


async def test_cancelled_create_records_uid_before_propagating(cluster, capability, scope, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    create = cluster.create_namespaced_custom_object

    async def slow(**kwargs):
        entered.set()
        await release.wait()
        return await create(**kwargs)

    monkeypatch.setattr(cluster, "create_namespaced_custom_object", slow)
    backend = new(capability, scope)
    task = asyncio.create_task(backend.working_dir())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.ref is not None and len(cluster.claims) == 1


async def test_binary_files_argv_env_and_distinct_credentials(cluster, capability, scope, monkeypatch, tmp_path):
    observed = []

    async def execute(interpreter, request):
        observed.append(request)
        process = await asyncio.create_subprocess_exec(
            *request["argv"], cwd=tmp_path, env=request["env"] or None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        return {"outcome": "exited", "exit_code": process.returncode,
                "stdout": out.decode("utf-8", "replace"), "stderr": err.decode("utf-8", "replace")}

    monkeypatch.setattr(provider, "_execute", execute)
    backend = new(capability, scope)
    workspace = Workspace(backend)
    result = await backend.run(["/bin/sh", "-c", 'printf "%s" "$1$EXTRA"; exit 7', "sh", "literal;$HOME"],
                               env={"EXTRA": "!"})
    assert result.stdout == "literal;$HOME!" and result.exit_code == 7
    file = str(tmp_path / "quote's file")
    data = bytes(range(256)) * 3
    await workspace.write_bytes(file, data)
    assert await workspace.read_bytes(file) == data
    assert await capability.resume(scope, backend.ref).working_dir() == "/workspace"
    request = observed[0]
    digest = cluster.claims[backend.name]["spec"]["env"][0]["value"]
    assert hashlib.sha256(request["runtime_token"].encode()).hexdigest() == digest
    assert all(request["runtime_token"] not in repr(c) for c in cluster.claims.values())
    assert request["router_token"] != request["runtime_token"]
    assert request["uid"] == cluster.sandbox_uid and request["target"] == "router.example.test:443"


@pytest.mark.parametrize("outcome, error", [
    ("timeout", WorkspaceTimeoutError), ("truncated", WorkspaceOutputLimitError),
])
async def test_command_limits_keep_output(cluster, capability, scope, monkeypatch, outcome, error):
    submit = AsyncMock(return_value={"outcome": outcome, "stdout": "partial", "stderr": "", "exit_code": 137})
    monkeypatch.setattr(provider, "_execute", submit)
    with pytest.raises(error) as caught:
        await new(capability, scope).run("sleep 30", shell=True, timeout=.25)
    assert caught.value.stdout == "partial"
    request = submit.await_args.args[1]
    assert request["argv"] == ["/bin/sh", "-c", "sleep 30"] and request["timeout"] == .25


async def test_indeterminate_result_and_temporal_redelivery_never_repeat(cluster, capability, scope, monkeypatch):
    process = SimpleNamespace(
        returncode=1, communicate=AsyncMock(return_value=(b"", None)),
        wait=AsyncMock(), kill=lambda: None,
    )
    submit = AsyncMock(return_value=process)
    monkeypatch.setattr(provider.asyncio, "create_subprocess_exec", submit)
    backend = new(capability, scope)
    with pytest.raises(provider.CommandOutcomeUnknown, match="indeterminate"):
        await backend.run(["touch", "effect"])
    monkeypatch.setattr(provider.activity, "in_activity", lambda: True)
    monkeypatch.setattr(provider.activity, "info", lambda: SimpleNamespace(attempt=2))
    with pytest.raises(provider.CommandOutcomeUnknown, match="earlier activity"):
        await capability.resume(scope, backend.ref).run(["touch", "effect"])
    submit.assert_awaited_once()
    process.communicate.assert_awaited_once()


async def test_local_cancellation_closes_transport(cluster, capability, scope, monkeypatch):
    entered = asyncio.Event()

    async def block(_):
        entered.set()
        await asyncio.Event().wait()

    process = SimpleNamespace(communicate=block, wait=AsyncMock(), kill=Mock())
    submit = AsyncMock(return_value=process)
    monkeypatch.setattr(provider.asyncio, "create_subprocess_exec", submit)
    task = asyncio.create_task(new(capability, scope).run(["sleep", "99"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    submit.assert_awaited_once()
