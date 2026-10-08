import pytest

from app.slack_chat import slack_text, split_reply
from app.slack_tables import reply_parts


def parts(text):
    return reply_parts(text, slack_text, split_reply)


def tables(result):
    return [blocks[0] for _, blocks in result if blocks]


def test_table_preserves_columns_alignment_escaped_pipes_and_literal_mentions():
    result = parts('Before\n\n| Phase | Time |\n| :--- | ---: |\n| a \\| b & <@U12345678> | 3.32 |\n\nAfter')
    assert result[0] == ('Before\n\n', None)
    table = tables(result)[0]
    assert table['rows'][1] == [{'type': 'raw_text', 'text': 'a | b & <@U12345678>'},
                                {'type': 'raw_text', 'text': '3.32'}]
    assert table['column_settings'] == [{'align': 'left', 'is_wrapped': True},
                                       {'align': 'right', 'is_wrapped': True}]
    assert '&lt;@U12345678&gt;' in result[1][0]
    assert result[-1] == ('\nAfter', None)


@pytest.mark.parametrize('fence', ['```', '~~~~'])
def test_code_and_non_tables_stay_text(fence):
    text = f'{fence}\n| a | b |\n| --- | --- |\n| c | d |\n{fence}\nordinary | prose'
    assert not tables(parts(text))
    assert [text for text, _ in parts(text)] == split_reply(slack_text(text))


@pytest.mark.parametrize('row_count,width', [(205, 2), (40, 400)])
def test_large_tables_repeat_headers_without_losing_rows(row_count, width):
    data = [f'{i}' + 'x' * width for i in range(row_count)]
    result = parts('| Header |\n| --- |\n' + ''.join(f'| {value} |\n' for value in data))
    blocks = tables(result)
    assert len(blocks) > 1
    assert [row[0]['text'] for block in blocks for row in block['rows'][1:]] == data
    for block in blocks:
        assert block['rows'][0][0]['text'] == 'Header'
        assert len(block['rows']) <= 100
        assert sum(len(cell['text']) for row in block['rows'] for cell in row) <= 10000


def test_multiple_tables_and_unrenderable_tables_keep_content():
    table = 'A | B\n--- | ---\nx | y\n'
    assert len(tables(parts(table + '\nMiddle\n' + table))) == 2
    oversized = '| A |\n| --- |\n| ' + 'x' * 10001 + ' |'
    assert not tables(parts(oversized))
    assert ''.join(chunk for chunk, _ in parts(oversized)).count('x') == 10001
    wide = '|'.join(['a'] * 21) + '\n' + '|'.join(['---'] * 21)
    assert not tables(parts(wide))


def test_adjacent_tables_do_not_create_empty_messages():
    text = '| a |\n| --- |\n| b |\n\n| c |\n| --- |\n| d |\n\n'
    result = parts(text)
    assert len(result) == len(tables(result)) == 2
    assert all(fallback.strip() for fallback, _ in result)


@pytest.mark.parametrize('text', [
    '    | a | b |\n    | --- | --- |\n    | c | d |\n',
    '> | a | b |\n> | --- | --- |\n> | c | d |\n',
    '- item\n\n  | a | b |\n  | --- | --- |\n  | c | d |\n',
    '```\n```not-a-closing-fence\n| a | b |\n| --- | --- |\n| c | d |\n```',
])
def test_code_and_nested_contexts_preserve_original_text(text):
    assert parts(text) == [(chunk, None) for chunk in split_reply(slack_text(text))]


def test_parser_pads_missing_cells_and_stops_at_heading():
    result = parts('| A | B |\n| --- | --- |\n| x |\n# Next\nProse')
    assert tables(result)[0]['rows'][1] == [
        {'type': 'raw_text', 'text': 'x'}, {'type': 'raw_text', 'text': ''}]
    assert result[-1] == (slack_text('# Next\nProse'), None)


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_source_maps_preserve_text_with_unicode_line_separators(newline):
    before = 'Before\u2028still prose' + newline * 2
    after = newline + 'After'
    text = before + newline.join(['| A |', '| --- |', '| x |', '']) + after
    result = parts(text)
    assert result[0] == (slack_text(before), None)
    assert tables(result)[0]['rows'][1][0]['text'] == 'x'
    assert result[-1] == (slack_text(after), None)
