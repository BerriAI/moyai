"""Real SDK planning and local Git publication inputs, without provider writes."""

import base64
import re
import subprocess

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.planning import InMemoryPlanStore

from moyai.agent import WorkflowPlanning
from moyai.github_workspace_export import export


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
