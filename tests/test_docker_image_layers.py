import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.docker_smoke import verify_image_archive


def add_file(archive, name, data):
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    archive.addfile(entry, io.BytesIO(data))


class ImageArchiveTests(unittest.TestCase):
    def verify(self, layers, *, oci=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.tar'
            names = []
            with tarfile.open(path, 'w') as image:
                for index, files in enumerate(layers):
                    name = f'blobs/sha256/layer-{index}' if oci else f'{index}/layer.tar'
                    names.append(name)
                    data = io.BytesIO()
                    with tarfile.open(fileobj=data, mode='w') as layer:
                        for filename in files:
                            add_file(layer, filename, b'fixture')
                    add_file(image, name, data.getvalue())
                add_file(image, 'manifest.json', json.dumps([{'Layers': names}]).encode())
            return verify_image_archive(path)

    def test_whiteouts_do_not_hide_vendored_files_from_the_check(self):
        for oci in (False, True):
            with self.subTest(oci=oci), self.assertRaisesRegex(ValueError, 'pip/_vendor'):
                self.verify([
                    ['usr/local/lib/python3.13/site-packages/pip/_vendor/urllib3/__init__.py'],
                    ['usr/local/lib/python3.13/site-packages/.wh.pip'],
                ], oci=oci)

    def test_rejects_bootstrap_wheels_metadata_executables_and_caches(self):
        for name in (
            './usr/local/lib/python3.13/ensurepip/_bundled/pip-26.2.2-py3-none-any.whl',
            'usr/local/lib/python3.13/site-packages/pip-26.2.2.dist-info/METADATA',
            'usr/local/bin/pip3.13',
            'tmp/wheels/pip-26.2.2-py3-none-any.whl',
            'root/.cache/uv/archive-v0/build-dependency/__init__.py',
            'app/.venv/lib/python3.13/site-packages/distlib/w64-arm.exe',
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unused tooling'):
                self.verify([[name], ['root/.cache/.wh.uv']])

    def test_accepts_clean_runtime_and_application_layers(self):
        for oci in (False, True):
            with self.subTest(oci=oci):
                self.assertEqual(self.verify([
                    ['usr/local/bin/python', 'var/lib/dpkg/status', 'etc/ssl/certs/ca-certificates.crt'],
                    ['app/.venv/lib/python3.13/site-packages/certifi-2026.10.23.dist-info/METADATA'],
                ], oci=oci), 2)

    def test_rejects_an_empty_manifest(self):
        with self.assertRaisesRegex(ValueError, 'no layers'):
            self.verify([])


if __name__ == '__main__':
    unittest.main()
