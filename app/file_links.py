"""Local Markdown references become authenticated links to the saved file viewer."""
import re
from urllib.parse import quote, unquote


def markdown_reference(value: str) -> bool:
    if not value or len(value) > 1024 or re.search(r'%(?![0-9a-fA-F]{2})', value):
        return False
    try:
        path = unquote(value, errors='strict')
    except UnicodeDecodeError:
        return False
    path = path.removeprefix('sandbox:/workspace/').removeprefix('/workspace/').removeprefix('./')
    # Source line references use the same notation as the saved file viewer.
    suffix = re.search(r'(?::([1-9]\d*)(?::([1-9]\d*))?|#L([1-9]\d*)(?:-L([1-9]\d*))?)$', path)
    if suffix:
        numbers = [int(n) for n in suffix.groups() if n]
        if any(n > 2**53 - 1 for n in numbers) or (suffix[4] and int(suffix[4]) < int(suffix[3])):
            return False
        path = path[:suffix.start()]
    return (bool(path) and not re.search(r'[\\:#?\x00-\x1f\x7f]', path)
            and all(part not in {'', '.', '..'} for part in path.split('/'))
            and path.lower().endswith(('.md', '.markdown')))


def file_link(public_url: str, run_id: str, reference: str) -> str | None:
    if not public_url or not re.fullmatch(r'[a-f0-9]{32}', run_id) or not markdown_reference(reference):
        return None
    # Encode the reference itself, preserving percent escapes for the viewer's
    # existing single decode. No files or credentials are made public.
    url = f'{public_url.rstrip("/")}/#run={run_id}&file={quote(reference, safe="")}'
    return url if len(url) <= 1500 else None


def valid_file_return_path(value: str) -> bool:
    match = re.fullmatch(r'/#run=[a-f0-9]{32}&file=([^&#\s]+)', value)
    if not match:
        return False
    try:
        return markdown_reference(unquote(match[1], errors='strict'))
    except UnicodeDecodeError:
        return False
