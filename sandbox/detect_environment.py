"""Resolve repository setup inside an isolated build, after removing clone access."""
import hashlib
import json
from pathlib import Path
import platform
import re
import shlex
import sys
import tarfile
import tempfile
import urllib.request


class DetectionError(ValueError):
    pass


def read_json(path):
    if path.stat().st_size > 128000:
        raise DetectionError('Project configuration is too large.')
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise DetectionError('Project configuration must be valid JSON.') from None
    if not isinstance(value, dict):
        raise DetectionError('Project configuration must be an object.')
    return value


def detect(root, recipe):
    """Return commands, not success. Every command still runs before publication."""
    root = Path(root)
    custom = root / '.moyai/environment.json'
    if custom.exists():
        data = read_json(custom)
        allowed = {'setup', 'startup', 'verify', 'shutdown', 'instructions', 'apt_packages'}
        if set(data) - allowed or not isinstance(data.get('verify'), str) or not data['verify'].strip():
            raise DetectionError('.moyai/environment.json needs a verify command and only supported recipe fields.')
        for key in allowed - {'apt_packages'}:
            if key in data and (not isinstance(data[key], str) or len(data[key]) > 12000):
                raise DetectionError('Project recipe commands must be strings of at most 12000 characters.')
        packages = data.get('apt_packages', [])
        if (not isinstance(packages, list) or len(packages) > 40 or
                any(not isinstance(p, str) or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,79}', p) for p in packages)):
            raise DetectionError('Project recipe apt_packages must be Debian package names.')
        return {**recipe, **data, 'instructions': data.get('instructions', '') + '\nSetup source: .moyai/environment.json.'}
    if any((root / name).exists() for name in ('.devcontainer/devcontainer.json', '.devcontainer.json', 'devcontainer.json')):
        raise DetectionError('This repository uses a devcontainer. Add .moyai/environment.json or an admin recipe for its services; Docker/devcontainer execution is not supported automatically.')
    setup, verify, descriptions = [], [], []
    if (root / 'uv.lock').exists():
        setup += ['python -m pip install uv==0.12.22', 'uv sync --frozen']
        verify += ['uv pip check --python .venv/bin/python']
        descriptions += ['Python dependencies from uv.lock; use .venv/bin/python.']
    elif (root / 'poetry.lock').exists() or (root / 'Pipfile').exists():
        raise DetectionError('This Python package manager needs a setup recipe. Add .moyai/environment.json or edit the admin recipe.')
    elif (root / 'pyproject.toml').exists() or (root / 'requirements.txt').exists():
        setup += ['python -m pip install uv==0.12.22', 'uv venv --python ' + python_version(root)]
        if (root / 'requirements.txt').exists():
            setup += ['uv pip install --python .venv/bin/python -r requirements.txt']
            if (root / 'requirements-dev.txt').exists():
                setup += ['uv pip install --python .venv/bin/python -r requirements-dev.txt']
        else:
            setup += ['uv pip install --python .venv/bin/python -e .']
        verify += ['uv pip check --python .venv/bin/python']
        descriptions += ['Python dependencies installed in .venv; use .venv/bin/python.']
    if (root / 'package.json').exists():
        package = read_json(root / 'package.json')
        version = node_version(root, package)
        setup += ['python /opt/workspace-runner/sandbox/detect_environment.py install-node ' + shlex.quote(version)]
        prefix = 'export PATH="/opt/moyai-node/bin:$PATH"\n'
        manager = package.get('packageManager', '')
        if manager and not isinstance(manager, str):
            raise DetectionError('packageManager must be a string.')
        if (root / 'pnpm-lock.yaml').exists() or manager.startswith('pnpm@'):
            match = re.fullmatch(r'pnpm@(\d+\.\d+\.\d+)(?:\+sha\d+\.[a-fA-F0-9]+)?', manager)
            if not match or not (root / 'pnpm-lock.yaml').exists():
                raise DetectionError('Commit pnpm-lock.yaml and pin packageManager to pnpm@x.y.z.')
            setup += [prefix + 'npm install --global --prefix /opt/moyai-node pnpm@' + match[1], prefix + 'pnpm install --frozen-lockfile --config.engine-strict=true']
            verify += [prefix + 'pnpm list --depth=0']
        elif (root / 'yarn.lock').exists() or (manager and not manager.startswith('npm@')):
            raise DetectionError('This JavaScript package manager needs a setup recipe. Add .moyai/environment.json or edit the admin recipe.')
        else:
            if manager:
                match = re.fullmatch(r'npm@(\d+\.\d+\.\d+)(?:\+sha\d+\.[a-fA-F0-9]+)?', manager)
                if not match:
                    raise DetectionError('Pin packageManager to npm@x.y.z or use a custom recipe.')
                setup += [prefix + 'npm install --global --prefix /opt/moyai-node npm@' + match[1]]
            install = 'npm ci' if any((root / name).exists() for name in ('package-lock.json', 'npm-shrinkwrap.json')) else 'npm install'
            setup += [prefix + install + ' --engine-strict --no-audit --no-fund']
            verify += [prefix + 'npm ls --depth=0']
        descriptions += ['Node and package manager binaries are in /opt/moyai-node/bin. Dependencies installed; application services require project-specific startup commands.']
    if not setup:
        # Source-only repositories still get a validated checkout. Recognized
        # unsupported runtimes must not be presented as dependency-ready.
        if any((root / name).exists() for name in ('go.mod', 'Cargo.toml', 'pom.xml', 'build.gradle', 'Gemfile', 'composer.json', 'Dockerfile')):
            raise DetectionError('This runtime needs a setup recipe. Add .moyai/environment.json or edit the admin recipe.')
        descriptions += ['Source checkout and base tools are ready. No supported dependency manifest was found; configure a recipe before claiming dependencies or services are prepared.']
    return {**recipe, 'setup': '\n'.join(setup), 'verify': '\n'.join(verify + ['git rev-parse --verify HEAD']),
            'instructions': '\n'.join(descriptions) + '\nRepository: /workspace/repo. Dependencies match the saved source commit; refresh them after changing revisions. Run the actual project checks required by the task.'}


def python_version(root):
    path = root / '.python-version'
    value = path.read_text().strip() if path.exists() else '3.13'
    if not re.fullmatch(r'3\.\d+(?:\.\d+)?', value):
        raise DetectionError('Use a numeric .python-version or provide an explicit setup recipe.')
    return value


def node_version(root, package):
    for name in ('.nvmrc', '.node-version'):
        path = root / name
        if path.exists():
            value = path.read_text().strip().removeprefix('v')
            if value in {'node', 'lts/*'} or re.fullmatch(r'\d+(?:\.\d+){0,2}', value):
                return value
            raise DetectionError('Use a numeric Node version or lts/*, or provide an explicit setup recipe.')
    engines = package.get('engines') or {}
    value = engines.get('node', '') if isinstance(engines, dict) else ''
    # Common exact/caret major constraints; npm/pnpm validate all engine ranges.
    match = re.fullmatch(r'[~^]?(\d+)(?:\.(\d+|x|\*)){0,2}', str(value))
    return match[1] if match else 'lts/*'


def install_node(selector):
    """Install a verified official binary, resolving major/LTS to a concrete version."""
    if selector not in {'node', 'lts/*'} and not re.fullmatch(r'\d+(?:\.\d+){0,2}', selector):
        raise DetectionError('Unsupported Node version selector.')
    arch = {'x86_64': 'x64', 'aarch64': 'arm64'}.get(platform.machine())
    if not arch:
        raise DetectionError('No Node binary for this architecture.')
    with urllib.request.urlopen('https://nodejs.org/dist/index.json', timeout=30) as response:
        releases = json.load(response)
    candidates = [r for r in releases if (selector == 'node' or (selector == 'lts/*' and r.get('lts')) or
                  r['version'].removeprefix('v') == selector or r['version'].removeprefix('v').startswith(selector + '.'))
                  and 'linux-' + arch in r.get('files', [])]
    if not candidates:
        raise DetectionError('The requested Node release is unavailable.')
    version = candidates[0]['version']
    if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
        raise DetectionError('Invalid Node release metadata.')
    filename = f'node-{version}-linux-{arch}.tar.xz'
    base = 'https://nodejs.org/dist/' + version + '/'
    with urllib.request.urlopen(base + 'SHASUMS256.txt', timeout=30) as response:
        checksums = {line.split()[1]: line.split()[0] for line in response.read().decode().splitlines() if len(line.split()) == 2}
    destination = Path('/opt/moyai-node')
    if destination.exists():
        raise DetectionError('Node installation destination already exists.')
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / filename
        with urllib.request.urlopen(base + filename, timeout=120) as response, archive.open('wb') as output:
            while data := response.read(1024 * 1024):
                output.write(data)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != checksums.get(filename):
            raise DetectionError('Node archive checksum did not match.')
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter='data')
        destination.parent.mkdir(parents=True, exist_ok=True)
        (Path(tmp) / filename.removesuffix('.tar.xz')).rename(destination)
    print('Installed Node ' + version, flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'install-node':
        install_node(sys.argv[2])
    else:
        raise SystemExit('Expected install-node VERSION')
