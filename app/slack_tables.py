"""Convert parsed Markdown tables into bounded Slack Block Kit payloads."""
import re

from markdown_it import MarkdownIt


_MARKDOWN = MarkdownIt('commonmark').enable('table')


def reply_parts(text, format_text, split_text):
    """Return (fallback, blocks) pairs, with one bounded table per message.

    markdown-it-py handles table recognition, escaped pipes, alignment and code
    fences. Source maps preserve non-table text instead of re-rendering it.
    Nested tables stay in their original list/quote context as text. Cell text
    stays literal, so table content cannot trigger Slack mentions.
    """
    # Match Markdown's CR/LF line boundaries, not Unicode paragraph separators.
    lines = re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', text)[:-1]
    result, cursor = [], 0
    tokens = _MARKDOWN.parse(text)

    def emit_text(start, end):
        source = ''.join(lines[start:end])
        if source.strip():
            result.extend((chunk, None) for chunk in split_text(format_text(source)))

    for index, token in enumerate(tokens):
        if token.type != 'table_open' or token.level != 0:
            continue
        start, end = token.map
        rows, settings = [], []
        for child in tokens[index + 1:]:
            if child.type == 'table_close':
                break
            if child.type == 'tr_open':
                rows.append([])
            elif child.type == 'th_open':
                alignment = child.attrGet('style') or 'text-align:left'
                settings.append({'align': alignment.removeprefix('text-align:'), 'is_wrapped': True})
            elif child.type == 'inline':
                rows[-1].append(child.content)
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
