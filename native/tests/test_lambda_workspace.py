import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.workspaces import (
    CommandResult,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from moyai.activities import save_computer
from moyai.contracts import NewMessage
from moyai.lambda_workspace import LambdaBackend, LambdaProductComputer
from moyai.models import Session
from moyai.settings import Settings
from moyai.workspace_recovery import checkpoint, prepare_workspace

REFERENCE = {"provider": "lambda", "id": "lambda:us-east-1:microvm-original"}


def backend():
    return LambdaBackend(Settings(_env_file=None))


@pytest.mark.parametrize("method", ["write_bytes", "copy_from_local"])
async def test_product_binary_transfers_respect_linux_arguments_and_truncate(tmp_path, method):
    async def execute(command, **kwargs):
        assert max(len(arg.encode()) for arg in command) < 128 * 1024
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return CommandResult(exit_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())

    computer = LambdaProductComputer(Settings(_env_file=None), REFERENCE["id"])
    computer.backend = SimpleNamespace(run=execute)
    source, target = tmp_path / "source", tmp_path / "nested" / "quote's binary"
    data = bytes(range(256)) * 1536
    for value in (data, b"short", b""):
        if method == "write_bytes":
            await computer.write_bytes(value, str(target))
        else:
            source.write_bytes(value)
            await computer.copy_from_local(source, str(target))
        assert target.read_bytes() == value


async def test_concurrent_acquisition_creates_once_and_preserves_identity():
    value = backend()
    vm = SimpleNamespace(id=REFERENCE["id"])
    async def create(name, *, created):
        await asyncio.sleep(0)
        created(vm)
        return vm
    value.control.create = AsyncMock(side_effect=create)
    assert value.ref is None
    assert await asyncio.gather(value.working_dir(), value.working_dir()) == ["/workspace", "/workspace"]
    value.control.create.assert_awaited_once()
    assert value.ref == WorkspaceRef(**REFERENCE)


async def test_cancelled_acquisition_still_records_returned_identity():
    value = backend()
    entered, release = asyncio.Event(), asyncio.Event()
    async def create(name, *, created):
        entered.set()
        await release.wait()
        vm = SimpleNamespace(id=REFERENCE["id"])
        created(vm)
        return vm
    value.control.create = AsyncMock(side_effect=create)
    task = asyncio.create_task(value.working_dir())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert value.ref.id == REFERENCE["id"]


async def test_missing_attached_vm_never_creates_another():
    value = LambdaBackend(Settings(_env_file=None), ref=WorkspaceRef(**REFERENCE))
    value.control.get = AsyncMock(side_effect=WorkspaceUnavailableError("gone"))
    value.control.create = AsyncMock()
    with pytest.raises(WorkspaceUnavailableError):
        await value.run(["true"])
    value.control.create.assert_not_awaited()


@pytest.mark.parametrize("timed_out", [False, True])
async def test_command_exit_and_partial_output(timed_out):
    value = backend()
    result = {"state": "done", "exit_code": 7, "timed_out": timed_out,
              "stdout": base64.b64encode(b"result\xff").decode(), "stderr": ""}
    request = AsyncMock(side_effect=[{}, result, {}] if timed_out else [{}, result])
    value.vm = SimpleNamespace(request=request, running=AsyncMock(), lock=asyncio.Lock())
    if timed_out:
        with pytest.raises(WorkspaceTimeoutError) as error:
            await value.run(["false"], timeout=.1)
        assert error.value.stdout == "result\ufffd"
    else:
        output = await value.run(["false"])
        assert output.exit_code == 7 and output.stdout == "result\ufffd"


async def test_lambda_checkpoint_settlement_and_cold_followup_use_native_authority(runtime, actor, active, monkeypatch):
    session, turn, prepared = active
    monkeypatch.setattr("moyai.activities.current", lambda: runtime)
    conversation = ConversationTypeAdapter.dump_json(Conversation(
        conversation_id=prepared.scope.conversation_id,
    )).decode()
    assert await runtime.database.save_result(
        prepared.scope, conversation=conversation, output="42", workspace=REFERENCE,
        usage={}, checkpoint_workspace=True,
    )
    capture = AsyncMock(return_value="lambda:us-east-1:bucket:prefix/checkpoints/first.json")
    runtime.database.answers.snapshots = SimpleNamespace(capture=capture)
    await save_computer(turn.id, REFERENCE)
    async with runtime.database.sessions() as db:
        saved = await db.get(Session, session.id)
        assert checkpoint(saved.snapshot).workspace == REFERENCE
        assert saved.status == "idle" and not saved.save_warning
    followup = await runtime.database.submit(actor, session.id, NewMessage(prompt="Print it", request_key="next"))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(followup.id)
    create = AsyncMock(return_value=SimpleNamespace(id="lambda:us-east-1:microvm-restored"))
    control = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(alive=AsyncMock(return_value=False))), create=create,
    )
    monkeypatch.setattr("moyai.lambda_control.LambdaControl", lambda settings: control)
    result = await prepare_workspace(runtime, prepared)
    assert result.workspace["provider"] == "lambda"
    assert result.workspace["id"].endswith("microvm-restored")
    assert create.await_args.kwargs["checkpoint"].endswith("first.json")
    async with runtime.database.sessions() as db:
        saved = await db.get(Session, session.id)
        assert saved.snapshot is None and json.loads(saved.workspace) == result.workspace
