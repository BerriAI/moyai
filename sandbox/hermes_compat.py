"""Install reviewed compatibility patches before importing the pinned runtime.

Used both in the image build and at startup after restoring an older snapshot.
Already patched files are left alone; incompatible sources fail before tools run.
"""
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
import subprocess

PATCH_NAMES = ('hermes-steering.patch', 'hermes-stop-reason.patch')


def prepare_hermes_imports(source=None):
    """Expose Hermes internals without letting its `agent` package shadow Moyai."""
    import agent

    if source is None:
        entrypoint = find_spec('run_agent')
        if entrypoint is None or not entrypoint.origin:
            raise RuntimeError('The Hermes runtime is not installed in this environment.')
        source = Path(entrypoint.origin).parent
    vendor = Path(source).resolve() / 'agent'
    local = Path(__file__).resolve().parents[1] / 'agent'
    if Path(agent.__file__).resolve().parent != local:
        raise RuntimeError('Moyai must initialize its agent package before importing Hermes.')
    if not vendor.is_dir():
        raise RuntimeError('The Hermes runtime is missing its agent package.')

    def module_names(directory):
        return {path.stem for path in directory.iterdir()
                if path.name not in {'__init__.py', '__pycache__'}
                and (path.suffix == '.py' or path.is_dir())}

    collisions = module_names(local) & module_names(vendor)
    if collisions:
        raise RuntimeError('Hermes agent modules conflict with Moyai: ' + ', '.join(sorted(collisions)))
    if str(vendor) not in agent.__path__:
        agent.__path__.append(str(vendor))
    # This is the pinned Hermes package initializer's only side effect.
    import_module('agent.jiter_preload')


def apply_hermes_patches(source=Path('/opt/hermes')):
    pending = []
    for name in PATCH_NAMES:
        patch = Path(__file__).parent / name
        command = ['git', '-C', str(source), 'apply']
        if subprocess.run([*command, '--reverse', '--check', str(patch)], capture_output=True).returncode == 0:
            continue
        check = subprocess.run([*command, '--check', str(patch)], capture_output=True)
        if check.returncode != 0:
            raise RuntimeError(f'Hermes compatibility patch {patch.name} does not match this runtime. '
                               'Rebuild the environment with the supported Hermes revision.')
        pending.append(str(patch))
    if pending:
        subprocess.run(['git', '-C', str(source), 'apply', *pending], check=True, capture_output=True)


if __name__ == '__main__':
    apply_hermes_patches()
