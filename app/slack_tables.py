"""Convert Markdown tables before reply chunking loses their row structure."""
import re


def cells(line):
    value = line.strip()
    if '|' not in value:
        return None
    if value.startswith('|'):
        value = value[1:]
    if value.endswith('|') and not value.endswith(r'\|'):
        value = value[:-1]
    return [part.strip().replace(r'\|', '|') for part in re.split(r'(?<!\\)\|', value)]


def reply_parts(text, format_text, split_text):
    """Yield (fallback, blocks) with one bounded table per message.

    Cell text stays literal, so table content cannot trigger Slack mentions.
    Unsupported widths/oversized rows retain the existing text fallback.
    """
    lines = text.splitlines(keepends=True)
    pending, result = [], []

    def flush():
        if pending and ''.join(pending).strip():
            result.extend((chunk, None) for chunk in split_text(format_text(''.join(pending))))
        pending.clear()

    i, fence = 0, None
    while i < len(lines):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', lines[i])
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            pending.append(lines[i])
            i += 1
            continue
        header = cells(lines[i]) if fence is None else None
        separator = cells(lines[i + 1]) if header and i + 1 < len(lines) else None
        if not (separator and len(header) == len(separator) and len(header) <= 20
                and all(re.fullmatch(r':?-+:?', cell) for cell in separator)):
            pending.append(lines[i])
            i += 1
            continue
        end, rows = i + 2, [header]
        while end < len(lines):
            row = cells(lines[end])
            if row is None or len(row) != len(header):
                break
            rows.append(row)
            end += 1
        sizes = [sum(map(len, row)) for row in rows]
        if sizes[0] > 10000 or any(sizes[0] + size > 10000 for size in sizes[1:]):
            pending.extend(lines[i:end])
            i = end
            continue
        flush()
        settings = [{'align': 'center' if cell.startswith(':') and cell.endswith(':') else
                     'right' if cell.endswith(':') else 'left', 'is_wrapped': True} for cell in separator]

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
        i = end
    flush()
    return result or [(chunk, None) for chunk in split_text(format_text(text))]
