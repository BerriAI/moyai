"""Page complete libraries; shorten model views without shortening stored content."""
import base64
import hashlib
import json

from fastapi import HTTPException


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def page(items, cursor, size, identity):
    version = hashlib.sha256(encode(identity).encode()).hexdigest()
    offset = 0
    if cursor:
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor))
            offset = state['offset']
            if state['version'] != version:
                raise HTTPException(409, 'The library changed. Restart this search to enumerate the current version.')
            if type(offset) is not int or offset < 0:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, 'Invalid library cursor.') from None
    selected = items[offset:offset + size]
    end = offset + len(selected)
    return selected, {'next': base64.urlsafe_b64encode(encode({'version': version, 'offset': end}).encode()).decode()
                     if end < len(items) else None, 'total': len(items), 'version': version}


def fit_context(context, budget):
    """Conservative byte allocation supplied by ContextBudget, not a retention cap.

    Keep whole records. A large body gets a range-readable reference, never an
    unmarked prefix. The final gateway token counter still admits the request.
    """
    if not context or len(context.encode()) <= budget:
        return context
    heading, separator, raw = context.partition('\n')
    if not separator:
        return context
    data = json.loads(raw)
    fields = ('notes', 'loaded', 'matches', 'available', 'confirmed_repositories', 'source_message_ids')
    result = {key: value for key, value in data.items() if key not in fields}
    result['view'] = {'allocation_bytes': budget, 'omitted': {},
                      'instruction': 'Stored content is complete. Use the library search cursor or direct file/note ranges for omitted material.'}
    for field in fields:
        if field not in data:
            continue
        result[field] = []
        for item in data[field]:
            candidate = item
            trial = {**result, field: [*result[field], candidate]}
            if len((heading + '\n' + encode(trial)).encode()) > budget:
                if field == 'loaded':
                    candidate = {key: value for key, value in item.items() if key not in {'instructions', 'excerpts'}}
                    candidate.update(instructions_omitted=True, instructions_path='SKILL.md',
                                     instructions_characters=len(item.get('instructions', '')))
                    # Most recent requested ranges precede earlier ranges.
                    candidate['excerpts'] = []
                    for excerpt in reversed(item.get('excerpts', [])):
                        trial_item = {**candidate, 'excerpts': [*candidate['excerpts'], excerpt]}
                        if len((heading + '\n' + encode({**result, field: [*result[field], trial_item]})).encode()) <= budget:
                            candidate = trial_item
                elif field == 'notes':
                    candidate = {key: item[key] for key in ('id', 'key', 'revision', 'title', 'kind', 'repo_url') if key in item}
                    candidate.update(content_omitted=True, characters=item.get('characters', len(item.get('content', ''))))
            if len((heading + '\n' + encode({**result, field: [*result[field], candidate]})).encode()) <= budget:
                result[field].append(candidate)
            else:
                result['view']['omitted'][field] = result['view']['omitted'].get(field, 0) + 1
    # Omission counters themselves take space; include them in the final fit.
    for field in reversed(fields):
        while result.get(field) and len((heading + '\n' + encode(result)).encode()) > budget:
            result[field].pop()
            result['view']['omitted'][field] = result['view']['omitted'].get(field, 0) + 1
    if len((heading + '\n' + encode(result)).encode()) > budget:
        # A long orientation or scalar metadata must not defeat admission. This
        # contains no private content and sends discovery back to the tools.
        heading = 'Library context'
        result = {'notes': [], 'loaded': [], 'view': {'content_omitted': True,
                  'instruction': 'Use memory_search or skills_search and follow cursors; use direct reads for bodies.'}}
        if len((heading + '\n' + encode(result)).encode()) > budget:
            return ''
    return heading + '\n' + encode(result)
