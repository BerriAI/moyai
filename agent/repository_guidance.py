"""One directory-scoped AGENTS.md resolver shared by every harness."""
import hashlib
from pathlib import Path


def resolve(paths, *, workspace, source=None, offset=0, characters=None):
    root = Path(workspace).resolve()
    if source is not None:
        candidate = (root / source).resolve()
        if not candidate.is_relative_to(root) or candidate.name != 'AGENTS.md' or not candidate.is_file():
            raise ValueError('Guidance source must be an AGENTS.md inside this workspace.')
        text = candidate.read_text(encoding='utf-8')
        end = len(text) if characters is None else min(len(text), offset + characters)
        return {'path': str(candidate), 'scope': str(candidate.parent), 'content': text[offset:end],
                'characters': len(text), 'offset': offset, 'next_offset': end if end < len(text) else None,
                'sha256': hashlib.sha256(text.encode()).hexdigest()}
    result = []
    for path in paths:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError('Requested path is outside this workspace.')
        directory = target if target.is_dir() else target.parent
        ancestors = list(reversed([directory, *directory.parents]))
        sources = []
        for ancestor in ancestors:
            if not ancestor.is_relative_to(root):
                continue
            guide = ancestor / 'AGENTS.md'
            if guide.is_file() and guide.resolve().is_relative_to(root):
                sources.append(resolve([], workspace=root, source=str(guide), offset=offset, characters=characters))
        result.append({'target': str(target), 'guidance': sources})
    return {'paths': result, 'precedence': 'Deeper scope applies only to its descendants; current user/platform instructions take precedence.'}


TOOL = {'name': 'repository_guidance', 'description': 'Read the applicable AGENTS.md chain for workspace paths before editing. Reports each source, scope and hash in ancestor-to-descendant order. A source and range can retrieve a large guidance file.',
    'inputSchema': {'type': 'object', 'properties': {'paths': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
        'source': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
        'characters': {'type': 'integer', 'minimum': 1}}, 'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'idempotentHint': True}}
