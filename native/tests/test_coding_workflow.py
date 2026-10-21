"""Real SDK planning and local Git publication inputs, without provider writes."""

import base64
import hashlib
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.planning import InMemoryPlanStore

from moyai.agent import WorkflowPlanning
from moyai.github_workspace_export import MissingBaseCommit, export
from moyai.github_workspace_import import import_base


@pytest.mark.parametrize("current_history", [False, True])
async def test_planning_ids_do_not_add_a_durable_read_to_old_workflow_histories(monkeypatch, current_history):
    from moyai import agent as agent_module

    markers = []

    def patched(marker):
        markers.append(marker)
        return current_history

    monkeypatch.setattr(agent_module, "workflow", SimpleNamespace(in_workflow=lambda: True, patched=patched))
    store = InMemoryPlanStore()
    observed = []

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("write_plan", {"items": [
                {"content": "Keep this running task", "status": "in_progress"},
            ]})])
        observed.append(results[-1].content)
        return ModelResponse(parts=[TextPart("Done")])

    result = await Agent(FunctionModel(model), capabilities=[WorkflowPlanning(store=store)]).run("Create a plan.")
    assert result.output == "Done"
    assert markers == ["moyai-planning-persisted-ids-v1"]
    tasks = await store.get_items()
    assert len(tasks) == 1
    assert (tasks[0].id in observed[0]) is current_history
    assert observed[0].startswith("Plan updated.\n\n" if current_history else "Plan updated:")


async def test_write_plan_exposes_ids_that_the_next_real_sdk_call_can_update():
    store = InMemoryPlanStore()
    observed = []

    async def model(messages, _info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("write_plan", {"items": [
                {"content": "Inspect changes", "status": "in_progress"},
            ]})])
        observed.append(results[-1].content)
        if results[-1].tool_name == "write_plan":
            actual_id = re.search(r"\[([0-9a-f]{8})\]", results[-1].content).group(1)
            return ModelResponse(parts=[ToolCallPart("update_task_status", {
                "task_id": actual_id, "status": "completed",
            })])
        return ModelResponse(parts=[TextPart("Done")])

    result = await Agent(FunctionModel(model), capabilities=[
        WorkflowPlanning(store=store, enable_subtasks=True),
    ]).run("Inspect the change and mark the task complete.")
    assert result.output == "Done"
    tasks = await store.get_items()
    assert len(tasks) == 1 and tasks[0].status == "completed"
    assert tasks[0].id in observed[0]
    assert not any("not found" in value.lower() for value in observed)


@pytest.fixture
def repository(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    (tmp_path / "original.txt").write_text("before\n")
    (tmp_path / "deleted.txt").write_text("remove me\n")
    git("add", ".")
    git("commit", "-qm", "Base")
    return tmp_path, git, git("rev-parse", "HEAD")


def test_staged_export_preserves_binary_modes_deletions_and_ignores_unstaged_changes(repository):
    root, git, base = repository
    (root / "original.txt").write_text("staged\n")
    (root / "binary.bin").write_bytes(b"\x00\xff\x80data")
    script = root / "script.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    (root / "deleted.txt").unlink()
    git("add", "-A")
    (root / "original.txt").write_text("unstaged\n")
    (root / "untracked.txt").write_text("do not publish")
    files = {file["path"]: file for file in export(str(root.resolve()), base)}
    assert set(files) == {"original.txt", "binary.bin", "script.sh", "deleted.txt"}
    assert base64.b64decode(files["original.txt"]["content"]) == b"staged\n"
    assert base64.b64decode(files["binary.bin"]["content"]) == b"\x00\xff\x80data"
    assert files["script.sh"]["executable"]
    assert files["deleted.txt"]["content"] is None


def test_staged_export_rejects_symlinks(repository):
    root, git, base = repository
    (root / "link").symlink_to("original.txt")
    git("add", "link")
    with pytest.raises(ValueError, match="Symlinks"):
        export(str(root.resolve()), base)


def test_staged_export_includes_commits_after_the_requested_base(repository):
    root, git, base = repository
    (root / "original.txt").write_text("committed edit\n")
    git("add", "original.txt")
    git("commit", "-qm", "Edit")
    files = export(str(root.resolve()), base)
    assert len(files) == 1
    assert base64.b64decode(files[0]["content"]) == b"committed edit\n"


def test_staged_export_rejects_an_unmerged_index(repository):
    root, git, base = repository
    git("checkout", "-qb", "other")
    (root / "original.txt").write_text("other change\n")
    git("commit", "-qam", "Other")
    git("checkout", "-qb", "current", base)
    (root / "original.txt").write_text("current change\n")
    git("commit", "-qam", "Current")
    with pytest.raises(subprocess.CalledProcessError):
        git("merge", "other")
    with pytest.raises(subprocess.CalledProcessError):
        export(str(root.resolve()), base)


@pytest.mark.parametrize("bad_digest", [False, True])
def test_import_published_base_preserves_all_local_work_and_refs(repository, tmp_path, bad_digest):
    source, git, base = repository
    checkout = tmp_path / "checkout"
    git("clone", "--no-hardlinks", str(source), str(checkout))
    (source / "original.txt").write_text("first publication\n")
    git("commit", "-qam", "Published by the server")
    published = git("rev-parse", "HEAD")
    bundle = Path("/tmp/moyai-git-" + uuid4().hex + ".bundle")
    git("bundle", "create", str(bundle), "HEAD")

    def local(*args):
        return subprocess.check_output(["git", *args], cwd=checkout).decode().strip()

    (checkout / "original.txt").write_text("second staged publication\n")
    local("add", "original.txt")
    (checkout / "original.txt").write_text("unfinished unstaged edit\n")
    (checkout / "untracked.txt").write_text("untracked work\n")
    local("write-tree")  # Populate Git's tree cache before comparing index bytes.
    index = (checkout / ".git/index").read_bytes()
    refs = local("show-ref")
    (checkout / ".git/FETCH_HEAD").write_text("retain previous fetch metadata\n")
    with pytest.raises(MissingBaseCommit):
        export(str(checkout.resolve()), published)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if bad_digest:
        with pytest.raises(ValueError, match="integrity"):
            import_base(str(checkout.resolve()), str(bundle), "0" * 64, published)
        with pytest.raises(MissingBaseCommit):
            export(str(checkout.resolve()), published)
    else:
        import_base(str(checkout.resolve()), str(bundle), digest, published)
        files = export(str(checkout.resolve()), published)
        assert len(files) == 1 and files[0]["path"] == "original.txt"
        assert base64.b64decode(files[0]["content"]) == b"second staged publication\n"
    assert local("rev-parse", "HEAD") == base
    assert local("show-ref") == refs
    assert (checkout / ".git/index").read_bytes() == index
    assert (checkout / ".git/FETCH_HEAD").read_text() == "retain previous fetch metadata\n"
    assert (checkout / "original.txt").read_text() == "unfinished unstaged edit\n"
    assert (checkout / "untracked.txt").read_text() == "untracked work\n"
    assert not bundle.exists()
