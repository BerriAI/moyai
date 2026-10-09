"""Snapshot upgrades must prepare a usable pinned runtime before any agent starts."""
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sandbox import harness_dependencies as dependencies


@pytest.mark.parametrize('existing', ['', '0.99.0', 'broken', '1.1.0'])
def test_pi_installer_recovers_missing_outdated_and_broken_binaries(tmp_path, monkeypatch, existing):
    root, launcher = tmp_path / 'private node', tmp_path / 'bin/pi'
    monkeypatch.setattr(dependencies, 'PI_ROOT', root)
    monkeypatch.setattr(dependencies, 'PI_LAUNCHER', launcher)
    monkeypatch.setattr(dependencies.shutil, 'which', lambda name: str(launcher) if existing else None)
    versions = iter([existing, dependencies.PI_VERSION] if existing else [dependencies.PI_VERSION])
    monkeypatch.setattr(dependencies, 'runtime_version', lambda name: next(versions))
    commands = []
    monkeypatch.setattr(dependencies.subprocess, 'run', lambda command, **kwargs: commands.append(command))
    dependencies.prepare_binary('pi')
    if existing == dependencies.PI_VERSION:
        assert commands == [] and not launcher.exists()
    else:
        assert commands == [['npm', 'install', '--prefix', str(root), '--no-audit', '--no-fund',
            'node@' + dependencies.PI_NODE_VERSION, '@earendil-works/pi-coding-agent@' + dependencies.PI_VERSION]]
        assert launcher.stat().st_mode & 0o111
        assert str(root / 'node_modules/node/bin/node') in launcher.read_text()
        assert '"$@"' in launcher.read_text()
        # An independent shell verifies quoting and argument forwarding.
        node = root / 'node_modules/node/bin/node'
        node.parent.mkdir(parents=True)
        node.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        node.chmod(0o755)
        # Restore run for this real launcher check only.
        monkeypatch.undo()
        result = subprocess.check_output([str(launcher), 'argument with spaces'], text=True)
        assert result.splitlines() == [str(root / 'node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js'),
                                     'argument with spaces']


@pytest.mark.parametrize('failure', ['npm', 'verification'])
def test_pi_install_failure_blocks_launch(tmp_path, monkeypatch, failure):
    launcher = tmp_path / 'pi'
    launcher.write_text('previous launcher')
    monkeypatch.setattr(dependencies, 'PI_ROOT', tmp_path / 'runtime')
    monkeypatch.setattr(dependencies, 'PI_LAUNCHER', launcher)
    monkeypatch.setattr(dependencies.shutil, 'which', lambda name: None)
    monkeypatch.setattr(dependencies, 'runtime_version', lambda name: '')
    def install(command, **kwargs):
        if failure == 'npm':
            raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(dependencies.subprocess, 'run', install)
    with pytest.raises(subprocess.CalledProcessError if failure == 'npm' else RuntimeError):
        dependencies.prepare_binary('pi')
    if failure == 'npm':
        assert launcher.read_text() == 'previous launcher'


@pytest.mark.parametrize('outdated', ['source', 'mcp', 'both'])
@pytest.mark.parametrize('install_fails', [False, True])
def test_old_workspace_upgrades_before_import(tmp_path, monkeypatch, outdated, install_fails):
    source = tmp_path / 'litellm-source'
    (source / 'litellm/harness').mkdir(parents=True)
    (source / 'litellm/__init__.py').write_text('Harness = "pinned"\naagent_session = None\n')
    monkeypatch.setattr(dependencies, 'LITELLM_SOURCE', source)
    monkeypatch.setattr(dependencies, 'version', lambda name: '1.30.0' if outdated in {'mcp', 'both'} else dependencies.MCP_VERSION)
    monkeypatch.setattr(dependencies, 'ensure_pip', lambda: None)
    monkeypatch.syspath_prepend(str(source))
    for name in list(sys.modules):
        if name == 'litellm' or name.startswith('litellm.'):
            monkeypatch.delitem(sys.modules, name)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command[1:4] == ['-m', 'pip', 'install'] and install_fails:
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0, stdout='old-source' if outdated in {'source', 'both'} else dependencies.LITELLM_REVISION)
    monkeypatch.setattr(dependencies.subprocess, 'run', run)
    try:
        if install_fails:
            with pytest.raises(subprocess.CalledProcessError):
                dependencies.prepare_runtime()
            assert not any('checkout' in command for command in commands)
        else:
            dependencies.prepare_runtime()
            import litellm
            assert litellm.Harness == 'pinned'
            assert any('checkout' in command for command in commands) is (outdated in {'source', 'both'})
        assert any('mcp==' + dependencies.MCP_VERSION in command for command in commands)
    finally:
        sys.modules.pop('litellm', None)
