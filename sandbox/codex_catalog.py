"""Use Codex's native search alongside programmatic tool execution."""
import json
from pathlib import Path
import subprocess


def search_catalog(binary, home, model, env, *, cached=None):
    # Read the exact pinned binary's bundled metadata, without network discovery
    # or a copy of its model prompts in Moyai's source tree.
    if cached is not None:
        catalog = json.loads(Path(cached).read_text())
    else:
        result = subprocess.run([str(binary), 'debug', 'models', '--bundled'],
                                env=env, capture_output=True, text=True, check=True, timeout=10)
        catalog = json.loads(result.stdout)
    for descriptor in catalog['models']:
        if descriptor['slug'] != model:
            continue
        if not descriptor.get('supports_search_tool') or descriptor.get('tool_mode') != 'code_mode_only':
            return None
        # In 0.161.0, code_mode_only hides tool_search and the code executor
        # cannot invoke it. Mixed code mode keeps exec and exposes native search.
        descriptor['tool_mode'] = 'code_mode'
        path = Path(home) / 'models.json'
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(catalog))
        return str(path)
    return None
