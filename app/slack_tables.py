"""Convert parsed Markdown tables into bounded Slack Block Kit payloads."""
import re

def cells(line):
    """Split table cells, removing the escape immediately before a literal pipe."""
    value = line.strip()
    if '|' not in value:
        return None
    parts = re.split(r'(?<!\\)\|', value)
    if value.startswith('|'):
        parts.pop(0)
    if value.endswith('|') and not value.endswith(r'\|'):
        parts.pop()
    return [part.strip().replace(r'\|', '|') for part in parts]


def extracted_tables(lines):
    """Recognize unindented top-level tables; leave ambiguous contexts as text.

    Cell splitting/alignment were compared with markdown-it-py's table rule.
    This deliberately avoids implementing a full Markdown block parser.
    """
    fence = None
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\r\n')
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', line):
                fence = None
            i += 1
            continue
        if marker and (marker[1][0] == '~' or '`' not in marker[2]):
            fence = (marker[1][0], len(marker[1]))
            i += 1
            continue
        # Indentation may denote code or a list continuation; prefer plain text.
        if not line or line[0].isspace() or re.match(r'^(?:>|#|<|[-+*] |\d+[.)] )', line):
            i += 1
            continue
        header = cells(line)
        delimiter = cells(lines[i + 1]) if i + 1 < len(lines) and not lines[i + 1][0].isspace() else None
        if not header or not delimiter or len(header) != len(delimiter) or not all(
                re.fullmatch(r':?-+:?', cell) for cell in delimiter):
            i += 1
            continue
        settings = [{'align': 'center' if cell.startswith(':') and cell.endswith(':')
                     else 'right' if cell.endswith(':') else 'left', 'is_wrapped': True}
                    for cell in delimiter]
        start, i = i, i + 2
        rows = [header]
        while i < len(lines):
            line = lines[i].rstrip('\r\n')
            if not line or line[0].isspace() or re.match(r'^(?:>|#|<|`{3,}|~{3,}|[-+*] |\d+[.)] )', line):
                break
            row = cells(line)
            if row is None:
                break
            rows.append((row + [''] * len(header))[:len(header)])
            i += 1
        yield start, i, rows, settings


def reply_parts(text, format_text, split_text):
    """Return bounded table payloads, preserving unsupported Markdown as text."""
    # Match Markdown's CR/LF line boundaries, not Unicode paragraph separators.
    lines = re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', text)[:-1]
    result, cursor = [], 0

    def emit_text(start, end):
        source = ''.join(lines[start:end])
        if source.strip():
            result.extend((chunk, None) for chunk in split_text(format_text(source)))

    for start, end, rows, settings in extracted_tables(lines):
        header = rows[0]
        sizes = [sum(map(len, row)) for row in rows]
        if (len(header) > 20 or sizes[0] > 10000
                or any(sizes[0] + size > 10000 for size in sizes[1:])):
            continue
        emit_text(cursor, start)

        def emit(batch):
            block = {'type': 'table', 'column_settings': settings,
                     'rows': [[{'type': 'raw_text', 'text': cell} for cell in row] for row in batch]}
            fallback = '\n'.join(' | '.join(row) for row in batch)
            result.append((format_text(fallback) if fallback.strip() else 'Table', [block]))

        batch, size = [header], sizes[0]
        for row, row_size in zip(rows[1:], sizes[1:]):
            if len(batch) == 100 or size + row_size > 10000:
                emit(batch)
                batch, size = [header], sizes[0]
            batch.append(row)
            size += row_size
        emit(batch)
        cursor = end
    emit_text(cursor, len(lines))
    return result or [(chunk, None) for chunk in split_text(format_text(text))]
