import subprocess

import pytest
from scripts import node_runtime_smoke


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
@pytest.mark.parametrize("results", [(0,), (1, 0), (1, 1), (None, 0), (None, None)])
def test_base_image_pull_uses_alternate_registry_without_skipping_failure(monkeypatch, platform, results):
    calls = []

    def run(command, *, check, timeout):
        assert check is False
        assert timeout == 120
        assert command[:4] == ["docker", "pull", "--platform", platform]
        calls.append(command[-1])
        result = results[len(calls) - 1]
        if result is None:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, result)

    monkeypatch.setattr(node_runtime_smoke.subprocess, "run", run)
    if results[-1] == 0:
        assert node_runtime_smoke.pull_base_image(platform) == node_runtime_smoke.BASE_IMAGES[len(results) - 1]
    else:
        with pytest.raises(RuntimeError, match="Could not pull"):
            node_runtime_smoke.pull_base_image(platform)
    assert calls == list(node_runtime_smoke.BASE_IMAGES[:len(results)])
