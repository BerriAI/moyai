"""Bounded, inert text views of Slack's generated Canvas HTML exports."""
import codecs
from html.parser import HTMLParser
import re


CANVAS_MIME = 'application/vnd.slack-docs'
PREVIEW_CHARACTERS = 8000
PREVIEW_SOURCE_BYTES = 256 * 1024
PARSE_CHUNK_BYTES = 16 * 1024
TRUNCATION_NOTICE = '\n\n[Canvas text preview truncated. The complete original HTML is retained in the attachment.]'
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
IGNORED = {'head', 'script', 'style', 'noscript', 'template', 'iframe', 'object', 'svg', 'math', 'canvas', 'form'}


class CanvasFormatError(ValueError):
    """Never contains provider HTML, file names, attributes or URLs."""


def is_canvas(file):
    return (file.get('mimetype') == CANVAS_MIME
            and file.get('filetype') == 'quip' and file.get('mode') == 'quip')


def bounded_preview(text, *, truncated=False):
    if truncated or len(text) > PREVIEW_CHARACTERS:
        return text[:PREVIEW_CHARACTERS-len(TRUNCATION_NOTICE)].rstrip() + TRUNCATION_NOTICE
    return text


class CanvasText(HTMLParser):
    """Extract visible text only; never evaluate HTML/CSS or resolve resources."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.frames, self.lists, self.items, self.parts = [], [], [], []
        self.root_depth = None
        self.root_closed = self.invalid = self.truncated = False
        self.length = 0

    def append(self, text):
        remaining = PREVIEW_CHARACTERS + 1 - self.length
        if len(text) > remaining:
            self.truncated = True
        if remaining > 0:
            self.parts.append(text[:remaining])
            self.length += len(self.parts[-1])
            return len(self.parts)-1
        return None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get('class') or '').split()
        hidden = bool(self.frames and self.frames[-1][1]) or tag in IGNORED
        hidden = hidden or 'hidden' in attrs or attrs.get('aria-hidden') == 'true'
        hidden = hidden or bool(re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', attrs.get('style') or '', re.I))
        checklist = bool(self.frames and self.frames[-1][2]) or attrs.get('data-section-style') == '7' or 'checklist' in classes
        if tag == 'div' and 'quip-canvas-content' in classes:
            if self.root_depth is not None:
                self.invalid = True
            else:
                self.root_depth = len(self.frames)+1
        inside = self.root_depth is not None and not self.root_closed and len(self.frames)+1 >= self.root_depth
        if tag not in VOID:
            self.frames.append((tag, hidden, checklist))
        if inside and (tag == 'form' or (tag == 'input' and attrs.get('type', '').lower() == 'password')):
            self.invalid = True
        if not inside or hidden:
            return
        if re.fullmatch(r'h[1-6]', tag):
            self.append('\n\n' + '#' * int(tag[1]) + ' ')
        elif tag in {'p', 'div', 'section', 'article', 'blockquote', 'tr'}:
            self.append('\n')
        elif tag in {'br', 'hr'}:
            self.append('\n')
        elif tag in {'ul', 'ol'}:
            self.append('\n')
            self.lists.append({'ordered': tag == 'ol', 'number': 0, 'checklist': checklist})
        elif tag == 'li':
            self.append('\n')
            listing = self.lists[-1] if self.lists else {'ordered': False, 'number': 0, 'checklist': False}
            listing['number'] += 1
            base = '  ' * min(max(0, len(self.lists)-1), 8) + (f'{listing["number"]}. ' if listing['ordered'] else '- ')
            state = attrs.get('data-checked', attrs.get('aria-checked'))
            task = listing['checklist'] or 'checked' in classes or state in {'true', 'false'}
            marker = '[x] ' if 'checked' in classes or state == 'true' else '[ ] '
            index = self.append(base + (marker if task else ''))
            self.items.append((index, base))
        elif tag == 'input' and attrs.get('type', '').lower() == 'checkbox':
            marker = '[x] ' if 'checked' in attrs or attrs.get('aria-checked') == 'true' else '[ ] '
            if self.items and self.items[-1][0] is not None:
                index, base = self.items[-1]
                previous = self.parts[index]
                self.parts[index] = base + marker
                self.length += len(self.parts[index])-len(previous)
            else:
                self.append(marker)
        elif tag in {'td', 'th'}:
            self.append(' | ')

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.frames)-1, -1, -1) if self.frames[i][0] == tag), None)
        if index is None:
            return
        visible = self.root_depth is not None and not self.root_closed and index+1 >= self.root_depth and not self.frames[index][1]
        if visible:
            if tag == 'li' and self.items:
                self.items.pop()
            if tag in {'ul', 'ol'} and self.lists:
                self.lists.pop()
            if tag in {'li', 'p', 'div', 'ul', 'ol', 'tr', 'section', 'article', 'blockquote'} or re.fullmatch(r'h[1-6]', tag):
                self.append('\n')
        if self.root_depth is not None and index+1 == self.root_depth:
            self.root_closed = True
        del self.frames[index:]

    def handle_data(self, data):
        if (self.root_depth is None or self.root_closed or len(self.frames) < self.root_depth
                or self.frames[-1][1]):
            return
        self.append(re.sub(r'\s+', ' ', data))


def inspect_canvas(raw):
    """Keep original bytes elsewhere; parse only a bounded UTF-8 export prefix."""
    parser = CanvasText()
    decoder = codecs.getincrementaldecoder('utf-8-sig')()
    raw.seek(0)
    consumed = 0
    try:
        while consumed < PREVIEW_SOURCE_BYTES:
            chunk = raw.read(min(PARSE_CHUNK_BYTES, PREVIEW_SOURCE_BYTES-consumed))
            if not chunk:
                break
            consumed += len(chunk)
            parser.feed(decoder.decode(chunk))
        truncated_source = bool(raw.read(1)) if consumed == PREVIEW_SOURCE_BYTES else False
        if not truncated_source:
            parser.feed(decoder.decode(b'', final=True))
            parser.close()
        text = re.sub(r'[ \t]+\n', '\n', ''.join(parser.parts))
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        if parser.invalid or parser.root_depth is None or not text:
            raise CanvasFormatError('The response was not a readable Canvas export.')
        if truncated_source:
            # A prefix cannot validate the whole HTML document. Require at least
            # the observed export's closing wrapper; label the preview partial.
            raw.seek(0, 2)
            end = raw.tell()
            raw.seek(max(0, end-256))
            if not re.search(rb'</div>\s*$', raw.read(256), re.I):
                raise CanvasFormatError('The Canvas export did not finish its wrapper.')
        elif not parser.root_closed:
            raise CanvasFormatError('The Canvas export did not finish its wrapper.')
        return CANVAS_MIME, b'', bounded_preview(text, truncated=truncated_source or parser.truncated)
    except UnicodeError:
        raise CanvasFormatError('The Canvas export was not valid UTF-8.') from None
    finally:
        raw.seek(0)
