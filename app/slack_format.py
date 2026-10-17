"""Pure Slack routing and formatting shared by both product cores."""
import html
import re

from .file_links import file_link

ID = re.compile(r"^[A-Z][A-Z0-9]{7,30}$")
STAMP = re.compile(r"^\d{10,16}\.\d{1,9}$")
ASIDE_PREFIXES = ("!aside", "(aside)")
CODE_SPAN = re.compile(r'(`[^`\n]+`)')
FENCED_CODE = re.compile(r'(```[\s\S]*?(?:```|\Z))')


def slack_markdown_blocks(text):
    """Adapt block syntax before inline formatting, leaving code bodies literal."""
    pieces = FENCED_CODE.split(text)
    for index, piece in enumerate(pieces):
        if index % 2:
            # mrkdwn has no fence info string; Slack displays it as code.
            pieces[index] = re.sub(r'\A```[^`\r\n]*\r?\n', '```\n', piece, count=1)
            continue

        def heading(match):
            # Format the complete heading, including inline code. Avoid nested
            # bold delimiters when the model also emphasizes heading words.
            spans = CODE_SPAN.split(match[1].strip())
            markers = [
                (span_index, marker.start())
                for span_index in range(0, len(spans), 2)
                for marker in re.finditer(r'\*\*', spans[span_index])
            ]
            for span_index, offset in reversed(markers[:len(markers) // 2 * 2]):
                spans[span_index] = spans[span_index][:offset] + spans[span_index][offset + 2:]
            return '*' + ''.join(spans) + '*'

        pieces[index] = re.sub(r'^#{1,6}[ \t]+(\S[^\r\n]*)', heading, piece, flags=re.MULTILINE)
    return ''.join(pieces)


def slack_text(text, mentions=frozenset(), *, public_url='', run_id=''):
    """Format replies without letting model text trigger Slack mentions/unfurls.

    Only user IDs in ``mentions`` (already mentioned by people in the thread)
    may render as mentions; broadcasts and every other ID stay literal text.
    """
    text = slack_markdown_blocks(html.escape(text, quote=False))
    def mention(match):
        return f'<@{match[1]}>' if match[1] in mentions else match[0]
    def link(match):
        label, url = match.groups()
        if not url.startswith(('https://', 'http://')):
            url = file_link(public_url, run_id, html.unescape(url))
            if not url:
                return match[0]
            url = html.escape(url, quote=False)
        if len(url) > 1500:
            return label
        return '<' + url + '|' + label.replace('|', '¦') + '>'
    # Preserve fenced examples; standalone inline Markdown filenames are links.
    pieces = re.split(r'(```[\s\S]*?(?:```|\Z)|`[^`\n]+`)', text)
    for i in range(0, len(pieces), 2):
        value = re.sub(r'!?\[([^\]\n]+)\]\(([^\s)<>]+)\)', link, pieces[i])
        value = re.sub(r'&lt;@([UW][A-Z0-9]{7,30})&gt;', mention, value)
        pieces[i] = value
    markers = [
        (index, match.start())
        for index in range(0, len(pieces), 2)
        for match in re.finditer(r'\*\*', pieces[index])
    ]
    paired_markers = markers[: len(markers) // 2 * 2]
    for index, offset in sorted(paired_markers, reverse=True):
        pieces[index] = pieces[index][:offset] + '*' + pieces[index][offset + 2 :]
    for i in range(1, len(pieces), 2):
        if pieces[i].startswith('```'):
            continue
        label = pieces[i][1:-1]
        url = file_link(public_url, run_id, html.unescape(label))
        if url:
            pieces[i] = '<' + html.escape(url, quote=False) + '|' + label.replace('|', '¦') + '>'
    return ''.join(pieces)


def split_reply(text, limit=2600):
    chunks, opened = [], False
    while text:
        end = len(text) if len(text) <= limit else text.rfind('\n', 0, limit)
        if end < limit // 2 and len(text) > limit:
            end = limit
        # A generated file URL may occupy most of a chunk. Keep Slack's link
        # token intact when a long paragraph would otherwise split it in two.
        for match in re.finditer(r'<https?://[^>]+>', text):
            if match.start() < end < match.end() and match.end() - match.start() <= limit:
                end = match.start() or match.end()
                break
        part, text = text[:end], text[end:].lstrip('\n')
        in_fence = opened ^ (part.count('```') % 2 == 1)
        chunks.append(('```\n' if opened else '') + part + ('\n```' if in_fence else ''))
        opened = in_fence
    return chunks or ['No response text was returned.']
