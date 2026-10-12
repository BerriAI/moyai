"""Real Moyai UI/API with synthetic saved files and a slow object-store double.

uv run python scripts/files_loading_demo.py --port 8892
Use --root /path/to/baseline --port 8893 for an identical baseline comparison.
No model or external storage calls; temporary data is removed when stopped.
"""
import argparse
import hashlib
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--port', type=int, default=8892)
parser.add_argument('--archive-delay', type=float, default=8)
parser.add_argument('--archive-failures', type=int, default=0, help='Fail this many initial archive reads to exercise Retry.')
args = parser.parse_args()
sys.path.insert(0, str(args.root.resolve()))

from fastapi.responses import HTMLResponse, RedirectResponse
from PIL import Image, ImageDraw
import uvicorn
from scripts.session_ui_demo import demo


class DemoObjects:
    enabled = True

    def __init__(self):
        self.values = {}
        self.archive = ''
        self.reads = []
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.failures = args.archive_failures

    def put(self, raw):
        reference = 'demo:' + hashlib.sha256(raw).hexdigest()
        self.values[reference] = raw
        return reference

    def read(self, reference, limit):
        raw = self.values[reference]
        assert len(raw) <= limit
        return raw

    def copy_to(self, reference, expected_size, stream):
        started = time.monotonic()
        if reference == self.archive:
            time.sleep(max(0, args.archive_delay))
            with self.lock:
                if self.failures > 0:
                    self.failures -= 1
                    raise OSError('Demo archive temporarily unavailable')
        raw = self.read(reference, expected_size)
        assert len(raw) == expected_size
        stream.write(raw)
        with self.lock:
            self.reads.append({'start_ms': round((started-self.started)*1000, 2),
                               'duration_ms': round((time.monotonic()-started)*1000, 2),
                               'bytes': len(raw), 'archive': reference == self.archive})

    def close(self):
        pass


def build(directory):
    app = demo(directory, args.port)
    store = app.state.store
    owner = store.identity({'method': 'google', 'identity': {
        'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex'}})
    run = store.create_run('Verify fast Files and chat links', '', 'demo', [], chat_enabled=True, user_id=owner)
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'],
        'The saved screenshot is ready: [Open result](sandbox:/workspace/moyai-captures/result.png).\n\n'
        'This is a local performance fixture. Its 8 MiB workspace archive takes '
        f'{args.archive_delay:g} seconds to retrieve. The screenshot should open while that archive is loading.\n\n'
        'Open **Files**, search for `result`, then open the capture. '
        'Clear the search after the workspace index arrives to browse its source files.')
    store.update_run(run['id'], status='idle')
    objects = store.objects = DemoObjects()
    raw = BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('new-files/video/render.bin', b'0' * (8 * 1024 * 1024))
        archive.writestr('new-files/README.md', '# Workspace result\n\nThe source files arrived after the capture.\n')
        archive.writestr('result.md', 'Saved fixture')
    store.artifacts.save(run['id'] + '.zip', raw.getvalue())
    objects.archive = store.artifacts.info(run['id'] + '.zip')['reference']
    picture = Image.new('RGB', (960, 540), '#0d1726')
    draw = ImageDraw.Draw(picture)
    draw.rounded_rectangle((70, 100, 890, 440), radius=25, outline='#b7cdfa', width=3)
    draw.text((115, 170), 'Saved capture is ready', fill='#eef5fb', font_size=48)
    draw.text((115, 250), 'Opened without waiting for the workspace ZIP.', fill='#a9bdce', font_size=29)
    draw.text((115, 320), 'Local synthetic file - real Moyai viewer', fill='#b7cdfa', font_size=27)
    png = BytesIO()
    picture.save(png, format='PNG')
    store.artifacts.save(run['id'] + '-captures/result.png', png.getvalue())

    @app.get('/demo/files-login')
    def login():
        response = RedirectResponse('/demo/files-workspace')
        app.state.security.new_session(response, identity={
            'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'})
        return response

    @app.get('/demo/files-workspace')
    def workspace():
        return HTMLResponse((args.root / 'app/static/index.html').read_text())

    @app.get('/demo/files-metrics')
    def metrics():
        return {'run_id': run['id'], 'archive_bytes': len(raw.getvalue()),
                'configured_archive_delay_seconds': args.archive_delay, 'reads': objects.reads}

    print(f'Local synthetic demo: http://127.0.0.1:{args.port}/demo/files-login', flush=True)
    print(f'Fixture session: {run["id"]}', flush=True)
    return app


with TemporaryDirectory(prefix='files-demo-', dir=Path(__file__).resolve().parents[2]) as directory:
    uvicorn.run(build(directory), host='127.0.0.1', port=args.port, log_level='warning')
