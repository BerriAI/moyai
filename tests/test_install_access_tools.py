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
    installer.ensure_tools('op whoami')
    installer.ensure_tools()
    monkeypatch.setattr(installer.shutil, 'which', lambda name: None)
    installer.ensure_tools('python provider_check.py')
