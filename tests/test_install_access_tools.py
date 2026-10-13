import hashlib
import io
import stat
import zipfile

import pytest

from sandbox import install_access_tools as installer


@pytest.mark.parametrize('machine,architecture', [('x86_64', 'amd64'), ('aarch64', 'arm64')])
def test_op_install_checks_release_then_extracts_only_binary(monkeypatch, tmp_path, machine, architecture):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('op', b'synthetic-op-binary')
        archive.writestr('../unwanted', b'must-not-extract')
    data = content.getvalue()
    monkeypatch.setattr(installer, 'INSTALL_DIR', tmp_path)
    monkeypatch.setattr(installer.shutil, 'which', lambda name: None)
    monkeypatch.setattr(installer.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(installer.platform, 'machine', lambda: machine)
    monkeypatch.setitem(installer.OP_SHA256, architecture, hashlib.sha256(data).hexdigest())
    urls = []
    def download(url):
        urls.append(url)
        return data
    monkeypatch.setattr(installer, 'download', download)
    installer.ensure_tools('op whoami && /usr/local/bin/op vault list')
    assert urls == [f'https://cache.agilebits.com/dist/1P/op2/pkg/v2.30.0/op_linux_{architecture}_v2.30.0.zip']
    assert (tmp_path / 'op').read_bytes() == b'synthetic-op-binary'
    assert stat.S_IMODE((tmp_path / 'op').stat().st_mode) == 0o755
    assert list(tmp_path.iterdir()) == [tmp_path / 'op']
    assert not (tmp_path.parent / 'unwanted').exists()


def test_bad_op_checksum_installs_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, 'INSTALL_DIR', tmp_path)
    monkeypatch.setattr(installer.shutil, 'which', lambda name: None)
    monkeypatch.setattr(installer.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(installer.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(installer, 'download', lambda url: b'wrong-release')
    with pytest.raises(ValueError, match='checksum mismatch'):
        installer.ensure_tools('op whoami')
    assert list(tmp_path.iterdir()) == []


def test_installed_tools_and_unrelated_commands_do_not_download(monkeypatch):
    def unexpected(url):
        raise AssertionError('must not download')
    monkeypatch.setattr(installer, 'download', unexpected)
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/usr/local/bin/' + name)
    monkeypatch.setattr(installer, 'aws_current', lambda: True)
    installer.ensure_tools('op whoami')
    installer.ensure_tools()
    monkeypatch.setattr(installer.shutil, 'which', lambda name: None)
    installer.ensure_tools('python provider_check.py')


@pytest.mark.parametrize('machine,architecture', [('x86_64', 'amd64'), ('aarch64', 'arm64')])
def test_aws_v1_is_upgraded_with_verified_official_bundle(monkeypatch, tmp_path, machine, architecture):
    from types import SimpleNamespace
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('aws/install', b'synthetic installer')
    data = content.getvalue()
    monkeypatch.setattr(installer, 'INSTALL_DIR', tmp_path)
    monkeypatch.setattr(installer, 'AWS_INSTALL_DIR', tmp_path / 'aws-cli')
    monkeypatch.setattr(installer.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(installer.platform, 'machine', lambda: machine)
    monkeypatch.setattr(installer.shutil, 'which', lambda name: str(tmp_path / name))
    monkeypatch.setitem(installer.AWS_SHA256, architecture, hashlib.sha256(data).hexdigest())
    urls, commands = [], []
    monkeypatch.setattr(installer, 'download', lambda url: urls.append(url) or data)
    version = b'aws-cli/1.42.30 Python/3.13'
    def run(args, **kwargs):
        nonlocal version
        commands.append(args)
        if '--update' in args:
            assert '--install-dir' in args and '--bin-dir' in args
            version = f'aws-cli/{installer.AWS_VERSION} Python/3.13'.encode()
        return SimpleNamespace(returncode=0, stdout=version)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    installer.ensure_tools('aws --version')
    installer.ensure_tools('aws sts get-caller-identity')
    assert len(urls) == 1 and f'linux-{machine}-{installer.AWS_VERSION}.zip' in urls[0]
    assert len([call for call in commands if '--update' in call]) == 1
    assert not any('pip' in call for call in commands)


def test_bad_aws_checksum_never_runs_installer(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, 'INSTALL_DIR', tmp_path)
    monkeypatch.setattr(installer, 'aws_current', lambda: False)
    monkeypatch.setattr(installer, 'download', lambda url: b'corrupt archive')
    with pytest.raises(ValueError, match='checksum mismatch'):
        installer.install_aws('amd64')
    assert not (tmp_path / 'aws').exists()


def test_already_installed_v2_repairs_shadowing_virtualenv_v1(monkeypatch, tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    legacy = tmp_path / 'venv/bin/aws'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('v1')
    installed = tmp_path / 'aws-cli/v2' / installer.AWS_VERSION / 'bin/aws'
    installed.parent.mkdir(parents=True)
    installed.write_text('v2')
    monkeypatch.setattr(installer, 'INSTALL_DIR', bin_dir)
    monkeypatch.setattr(installer, 'AWS_INSTALL_DIR', tmp_path / 'aws-cli')
    monkeypatch.setattr(installer, 'aws_current', lambda: legacy.is_symlink() and legacy.resolve() == installed)
    monkeypatch.setattr(installer.shutil, 'which', lambda name: str(legacy))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('aws/install', 'synthetic no-op installer')
    content = archive.getvalue()
    monkeypatch.setitem(installer.AWS_SHA256, 'amd64', hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(installer, 'download', lambda url: content)
    monkeypatch.setattr(installer.subprocess, 'run', lambda *args, **kwargs: None)
    installer.install_aws('amd64')
    assert legacy.is_symlink() and legacy.read_text() == 'v2'
