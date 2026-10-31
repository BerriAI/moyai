import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError

from moyai.lambda_control import LambdaControl, LambdaVM
from moyai.lambda_workspace import WorkspaceProviders
from moyai.settings import Settings


def settings(**kwargs):
    return Settings(
        _env_file=None, lambda_region="us-east-1", lambda_checkpoint_bucket="private-test",
        lambda_checkpoint_prefix="installation", **kwargs,
    )


@pytest.mark.parametrize("identity", [
    "lambda:us-west-2:private-test:installation/checkpoints/a.json",
    "lambda:us-east-1:other:installation/checkpoints/a.json",
    "lambda:us-east-1:private-test:installation-other/checkpoints/a.json",
    "lambda:us-east-1:private-test:installation/checkpoints/../a.json",
])
async def test_checkpoint_reference_cannot_cross_installation(identity):
    control = LambdaControl(settings())
    control.read = AsyncMock()
    with pytest.raises(ValueError):
        await control.manifest(identity)
    control.read.assert_not_awaited()


async def test_manifest_cannot_redirect_download_to_another_object():
    control = LambdaControl(settings())
    control.read = AsyncMock(return_value={
        "version": 1, "image": "pinned-image", "image_version": "1.0",
        "size": 1, "sha256": "0"*64, "key": "another-installation/secret.tar.gz",
    })
    with pytest.raises(ValueError):
        await control.manifest("lambda:us-east-1:private-test:installation/checkpoints/a.json")


@pytest.mark.parametrize("default,reference,expected", [
    ("lambda", WorkspaceRef(provider="modal", id="sb-existing"), "modal"),
    ("modal", WorkspaceRef(provider="lambda", id="lambda:us-east-1:microvm-old"), "lambda"),
    ("lambda", None, "lambda"),
    ("modal", None, "modal"),
])
def test_switching_default_does_not_switch_stored_workspace(default, reference, expected):
    modal = SimpleNamespace(get_workspace=lambda ctx, ref: "modal")
    providers = WorkspaceProviders(settings(sandbox_provider=default), modal)
    providers.lambda_sandbox = SimpleNamespace(get_workspace=lambda ctx, ref: "lambda")
    assert providers.get_workspace(SimpleNamespace(), ref=reference) == expected


async def test_archive_is_never_published_when_s3_checksum_disagrees():
    control = LambdaControl(settings())
    control.url = AsyncMock(return_value="https://scoped-test.invalid/object")
    control.aws = AsyncMock(return_value={"ContentLength": 3, "ChecksumSHA256": "wrong"})
    control.write = AsyncMock()
    vm = LambdaVM(control, {"microvmId": "test", "imageArn": "pinned", "imageVersion": "1.0"})
    vm.job = AsyncMock(side_effect=[
        {"size": 3, "sha256": "0"*64, "checksum": "expected", "diagnostics": {}}, {"uploaded": True},
    ])
    vm.request = AsyncMock(return_value={})
    with pytest.raises(RuntimeError, match="verification"):
        await vm.checkpoint()
    control.write.assert_not_awaited()
    assert vm.request.await_args.args[0] == "/checkpoint/finish"


async def test_checkpoint_serializes_with_native_commands():
    control = LambdaControl(settings())
    first = LambdaVM(control, {"microvmId": "shared"})
    second = LambdaVM(control, {"microvmId": "shared"})
    assert first.lock is second.lock
    entered = asyncio.Event()
    async def command():
        async with second.lock:
            entered.set()
    async with first.lock:
        task = asyncio.create_task(command())
        await asyncio.sleep(0)
        assert not entered.is_set()
    await task
    assert entered.is_set()


@pytest.mark.parametrize("conflict", [False, True])
async def test_suspending_workspace_resumes_same_vm_and_refreshes_token(monkeypatch, conflict):
    control = LambdaControl(settings())
    states = iter(["SUSPENDING", "SUSPENDED", "SUSPENDED", "RUNNING"])
    async def aws(service, method, **kwargs):
        assert kwargs["microvmIdentifier"] == "original"
        if method == "get_microvm":
            return {"microvmId": "original", "state": next(states)}
        assert method == "resume_microvm"
        if conflict:
            raise RuntimeError("AWS resume_microvm failed: ConflictException")
        return {}
    control.aws = AsyncMock(side_effect=aws)
    monkeypatch.setattr("moyai.lambda_control.asyncio.sleep", AsyncMock())
    vm = LambdaVM(control, {"microvmId": "original"})
    vm.auth, vm.auth_until = "expired-before-resume", 9999999999
    await vm.running()
    assert vm.id == "lambda:us-east-1:original"
    assert vm.auth == "" and vm.auth_until == 0
    assert [call.args[1] for call in control.aws.await_args_list].count("resume_microvm") == 1


@pytest.mark.parametrize("state", ["TERMINATED", "TERMINATING"])
async def test_expired_workspace_never_resumes_or_launches_in_transport(state):
    control = LambdaControl(settings())
    control.aws = AsyncMock(return_value={"microvmId": "original", "state": state})
    vm = LambdaVM(control, {"microvmId": "original"})
    with pytest.raises(WorkspaceUnavailableError):
        await vm.running()
    control.aws.assert_awaited_once_with("lambda-microvms", "get_microvm", microvmIdentifier="original")


async def test_resume_permission_failure_is_not_misclassified_as_workspace_loss():
    control = LambdaControl(settings())
    control.aws = AsyncMock(side_effect=[
        {"microvmId": "original", "state": "SUSPENDED"},
        RuntimeError("AWS resume_microvm failed: AccessDeniedException"),
    ])
    with pytest.raises(RuntimeError, match="AccessDeniedException"):
        await LambdaVM(control, {"microvmId": "original"}).running()


@pytest.mark.parametrize("idle,lifetime,expected", [(60, 300, (60, 300)), (86400, 86400, (28800, 28800))])
async def test_new_computers_suspend_on_idle_without_extending_total_lifetime(monkeypatch, idle, lifetime, expected):
    control = LambdaControl(settings(computer_idle_timeout=idle, computer_lifetime=lifetime))
    control.write = AsyncMock()
    control.aws = AsyncMock(return_value={"microvmId": "created"})
    monkeypatch.setattr(LambdaVM, "running", AsyncMock())
    monkeypatch.setattr(LambdaVM, "request", AsyncMock(return_value={"bootstrapped": True}))
    monkeypatch.setattr(LambdaVM, "job", AsyncMock(return_value={}))
    await control.create("session:first-turn")
    request = control.aws.await_args.kwargs
    assert request["idlePolicy"] == {
        "autoResumeEnabled": True, "maxIdleDurationSeconds": expected[0], "suspendedDurationSeconds": 28800,
    }
    assert request["maximumDurationInSeconds"] == expected[1]
