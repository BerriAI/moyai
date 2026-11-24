"""Fresh image identity is verified; admitted working computers remain editable."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from moyai.continuations.product_continuation_restore import ModalRecoveryTransport
from moyai.contracts import RunScope

SHA = "a" * 40
URL = "https://github.com/example/project"


def computer(commit=SHA, origin=URL, code=0):
    results = iter((commit, origin))
    return SimpleNamespace(
        exec=SimpleNamespace(aio=AsyncMock(side_effect=lambda *args, **kwargs: SimpleNamespace(
            returncode=code, wait=SimpleNamespace(aio=AsyncMock()),
            stdout=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=next(results)))),
        ))),
        poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
        terminate=SimpleNamespace(aio=AsyncMock()),
    )


@pytest.mark.parametrize("origin", [URL, URL + ".git", "https://github.com/Example/Project"])
async def test_fresh_prepared_checkout_matches_pinned_identity(origin):
    sandbox = computer(origin=origin)
    await ModalRecoveryTransport(None).verify_prepared_repository(sandbox, {"commit_sha": SHA, "repo_url": URL})
    assert sandbox.exec.aio.await_count == 2
    assert all(call.kwargs["timeout"] == 15 for call in sandbox.exec.aio.await_args_list)


@pytest.mark.parametrize("commit,origin,code", [
    ("b" * 40, URL, 0), (SHA, "https://github.com/example/other", 0),
    (SHA, "https://credential@example.test/group/project", 0), ("", "", 1),
    (SHA, URL + "\nhttps://github.com/example/other", 0),
])
async def test_wrong_or_unreadable_prepared_checkout_is_refused(commit, origin, code):
    with pytest.raises(ValueError) as raised:
        await ModalRecoveryTransport(None).verify_prepared_repository(
            computer(commit, origin, code), {"commit_sha": SHA, "repo_url": URL},
        )
    assert "credential" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("commit", [None, "", "main", "a" * 39, "A" * 40])
async def test_build_must_name_an_exact_commit(commit):
    sandbox = computer()
    with pytest.raises(ValueError, match="exact validated commit"):
        await ModalRecoveryTransport(None).verify_prepared_repository(sandbox, {"commit_sha": commit, "repo_url": URL})
    sandbox.exec.aio.assert_not_awaited()


async def test_recovered_unpublished_image_is_checked_and_bad_image_retired(monkeypatch):
    import modal

    sandbox = computer(commit="b" * 40)
    monkeypatch.setattr(modal.Sandbox.from_name, "aio", AsyncMock(return_value=sandbox))
    runtime = SimpleNamespace(settings=SimpleNamespace(sandbox_provider="modal", modal_app="test-app"))
    scope = RunScope("session", "turn", "actor", "model")
    with pytest.raises(ValueError, match="does not match"):
        await ModalRecoveryTransport(runtime).native(
            scope, {"environment_snapshot_id": "im-test", "commit_sha": SHA, "repo_url": URL}, None,
        )
    sandbox.terminate.aio.assert_awaited_once()


async def test_admitted_workspace_does_not_reset_or_reject_later_user_commits(monkeypatch):
    import modal

    sandbox = computer(commit="b" * 40)
    monkeypatch.setattr(modal.Sandbox.from_id, "aio", AsyncMock(return_value=sandbox))
    runtime = SimpleNamespace(settings=SimpleNamespace(sandbox_provider="modal"))
    result = await ModalRecoveryTransport(runtime).native(
        RunScope("session", "turn", "actor", "model"),
        {"environment_snapshot_id": "im-test", "commit_sha": SHA, "repo_url": URL},
        {"provider": "modal", "id": "sb-owned"},
    )
    assert result is sandbox
    sandbox.exec.aio.assert_not_awaited()
    sandbox.terminate.aio.assert_not_awaited()
