import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import docker_start
from docker_healthcheck import health_request


@pytest.mark.parametrize("render", [False, True])
def test_server_and_healthcheck_share_custom_port_and_origin(monkeypatch, render):
    monkeypatch.setenv("PORT", "12345")
    monkeypatch.setenv("PUBLIC_URL", "https://vm.example")
    if render:
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://render.example")
    else:
        monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    command = docker_start.server_command()
    if render:
        assert command == [sys.executable, str(Path(docker_start.__file__).with_name("render_start.py"))]
    else:
        assert command[command.index("--port") + 1] == "12345"
    request = health_request()
    assert request.full_url == "http://127.0.0.1:12345/health"
    assert request.get_header("Host") == ("render.example" if render else "vm.example")


@pytest.mark.parametrize("render,port", [(False, "8787"), (True, "10000")])
def test_healthcheck_uses_server_default_port(monkeypatch, render, port):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    if render:
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://render.example")
    assert health_request().full_url == f"http://127.0.0.1:{port}/health"


def test_root_drops_all_privileges_before_exec(monkeypatch, tmp_path):
    calls = []
    user = SimpleNamespace(pw_uid=10001, pw_gid=10001, pw_dir="/home/workspace")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["docker_start.py", "python", "custom.py"])
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(docker_start.pwd, "getpwnam", lambda _: user)
    monkeypatch.setattr(docker_start, "prepare_data_directory", lambda *args: calls.append(("prepare", args)))
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda value, name=name: calls.append((name, value)))
    monkeypatch.setattr(os, "execvp", lambda *args: calls.append(("exec", args)))
    docker_start.main()
    assert [name for name, _ in calls] == ["prepare", "setgroups", "setgid", "setuid", "exec"]
    assert calls[1:4] == [("setgroups", []), ("setgid", 10001), ("setuid", 10001)]
    assert calls[-1] == ("exec", ("python", ["python", "custom.py"]))
    assert os.environ["HOME"] == "/home/workspace"


def test_data_root_cannot_be_a_symlink_or_application_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    for directory in (link, Path("/"), Path("relative"), Path(docker_start.__file__).parent):
        with pytest.raises(ValueError, match="dedicated absolute directory"):
            docker_start.prepare_data_directory(directory, os.getuid(), os.getgid())
