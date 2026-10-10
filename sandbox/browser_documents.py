"""Complete browser observations and references to the exact observed DOM nodes.

Snapshots are views of a live computer, not durable authority or instructions.
Navigation can invalidate a target; it never retargets an action by array index.
"""
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4


ELEMENT = """e => ({tag:e.tagName,role:e.getAttribute('role'),
    name:e.getAttribute('aria-label')||Array.from(e.labels||[]).map(l=>l.innerText).join(' ')||e.innerText||e.getAttribute('placeholder')||e.getAttribute('alt')||'',
    type:e.getAttribute('type'),href:e.getAttribute('href'),disabled:!!e.disabled,
    options:e.tagName==='SELECT'?Array.from(e.options).map(o=>({value:o.value,label:o.label})):null})"""
CAPTURE = r"""() => {
    const describe = %s;
    const roots=[document],nodes=[],texts=[document.body?.innerText||''];
    for(let i=0;i<roots.length;i++) {
        for(const element of roots[i].querySelectorAll('*')) {
            if(element.matches('a,button,input,select,textarea,[role],[contenteditable=true]')) nodes.push(element);
            if(element.shadowRoot) {roots.push(element.shadowRoot);texts.push(element.shadowRoot.textContent||'');}
        }
    }
    return {text:texts.join('\n'),elements:nodes.map(describe),nodes};
}""" % ELEMENT


def window(items, offset, length):
    if type(offset) is not int or offset < 0 or length is not None and (type(length) is not int or length < 1):
        raise ValueError('Use a nonnegative offset and a positive range length.')
    end = len(items) if length is None else min(len(items), offset + length)
    return items[offset:end], end if end < len(items) else None


def workspace_file(path, workspace='/workspace'):
    root = Path(workspace).resolve()
    source = (root / path).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError('Upload an existing file inside the workspace.')
    return str(source)


class BrowserDocuments:
    def __init__(self):
        self.pages, self.frames, self.snapshots, self.targets = {}, {}, {}, {}

    @staticmethod
    def identity(mapping, value):
        key = next((key for key, item in mapping.items() if item is value), None)
        if key is None:
            key = uuid4().hex
            mapping[key] = value
        return key

    def page(self, context, identity):
        page = self.pages.get(identity)
        if page not in context.pages or page.is_closed():
            raise ValueError('This browser tab is no longer available. List tabs again.')
        return page

    async def tabs(self, context, current):
        return {'tabs': [{'id': self.identity(self.pages, page), 'url': page.url,
                          'title': await page.title(), 'active': page is current}
                         for page in context.pages if not page.is_closed()]}

    def frame(self, page, identity=''):
        frame = self.frames.get(identity) if identity else page.main_frame
        if frame not in page.frames:
            raise ValueError('This frame is no longer available. Read the page again.')
        return frame

    async def read(self, page, args):
        identity = args.get('snapshot')
        if identity:
            snapshot = self.snapshots.get(identity)
            if snapshot is None or snapshot['page'] is not page:
                raise ValueError('Snapshot is unavailable in this tab. Read a new snapshot.')
        else:
            frame = self.frame(page, args.get('frame', ''))
            captured = await frame.evaluate_handle(CAPTURE)
            try:
                data = await captured.evaluate('s => ({text:s.text,elements:s.elements})')
                nodes = await captured.get_property('nodes')
                handles = await nodes.get_properties()
                identity = uuid4().hex
                for number, item in enumerate(data['elements']):
                    target = identity + ':' + str(number)
                    self.targets[target] = {'page': page, 'frame': frame,
                        'element': handles[str(number)].as_element(), 'observed': dict(item)}
                    item['target'] = target
                snapshot = {**data, 'page': page, 'url': frame.url, 'title': await page.title(),
                    'observed_at': datetime.now(timezone.utc).isoformat(),
                    'frames': [{'id': self.identity(self.frames, source), 'url': source.url,
                                'name': source.name, 'main': source is page.main_frame} for source in page.frames]}
                self.snapshots[identity] = snapshot
                await nodes.dispose()
            finally:
                await captured.dispose()
        text, next_offset = window(snapshot['text'], args.get('offset', 0), args.get('characters'))
        elements, next_element = window(snapshot['elements'], args.get('element_offset', 0), args.get('element_count'))
        return {'url': snapshot['url'], 'title': snapshot['title'], 'snapshot': identity,
                'observed_at': snapshot['observed_at'],
                'text': text, 'characters': len(snapshot['text']), 'offset': args.get('offset', 0), 'next_offset': next_offset,
                'elements': elements, 'element_total': len(snapshot['elements']), 'next_element_offset': next_element,
                'frames': snapshot['frames'], 'coverage': 'One observed frame. Read listed frames independently; offscreen/lazy content may require scrolling.',
                'screenshot': 'Use browser_screenshot for visual inspection and a saved PNG.'}

    async def target(self, page, identity):
        target = self.targets.get(identity)
        if target is None or target['page'] is not page or target['frame'] not in page.frames:
            raise ValueError('Unknown or stale browser target. Read the current page/frame before acting.')
        element = target['element']
        try:
            valid = await element.evaluate('e => e.isConnected')
            metadata = await element.evaluate(ELEMENT) if valid else None
        except Exception:
            valid, metadata = False, None
        if not valid or metadata != target['observed']:
            raise ValueError('The observed target changed. Read the current page/frame before acting.')
        return element
