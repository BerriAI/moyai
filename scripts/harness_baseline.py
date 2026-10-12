"""Load historical Codex behavior while resolving its dependencies after file moves."""
import re
import subprocess
from types import ModuleType


MODULES = {
    'harness_agent': 'agent.harnesses.harness_agent',
    'codex_catalog': 'agent.harnesses.codex_catalog',
    'context_recovery': 'agent.context_recovery',
    'sdk_failure': 'agent.harnesses.sdk_failure',
    'transport_recovery': 'agent.transport_recovery',
}


def load_codex_baseline(root, revision):
    source = subprocess.check_output(
        ['git', 'show', revision + ':sandbox/codex_harness.py'], cwd=root, text=True)
    source = re.sub(r'from \.(\w+) import ',
        lambda match: 'from ' + MODULES.get(match[1], 'sandbox.' + match[1]) + ' import ', source)
    module = ModuleType('sandbox._codex_baseline')
    module.__file__ = str(root / 'sandbox/codex_harness.py')
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module
