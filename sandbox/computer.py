"""One loopback-only browser per sandbox, shared by MCP and the Computer panel.

The control plane uses Modal exec to reach this service; no public port, browser
debug endpoint, gateway credential or desktop token is exposed to the web UI.
"""
import asyncio
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import signal
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse
from uuid import uuid4

PORT = 18765
WIDTH, HEIGHT = 1280, 720
CAPTURES = Path('/workspace/moyai-captures')
MEDIA_LIMIT = 32 * 1024 * 1024
MEDIA_TOTAL = 64 * 1024 * 1024


def capture_name(label, extension):
    label = re.sub(r'[^a-zA-Z0-9_-]+', '-', str(label or 'capture')).strip('-')[:60] or 'capture'
    return f'{label}-{uuid4().hex[:12]}.{extension}'


def capture_directory():
    if CAPTURES.is_symlink() or (CAPTURES.exists() and not CAPTURES.is_dir()):
        raise ValueError('The capture folder must be a regular workspace directory.')
    CAPTURES.mkdir(parents=True, exist_ok=True)
    return CAPTURES


def capture_list():
    directory = capture_directory()
    return [{'name': p.name, 'size': p.stat().st_size, 'kind': 'video' if p.suffix == '.webm' else 'image'}
            for p in sorted(directory.iterdir(), reverse=True)
            if re.fullmatch(r'[A-Za-z0-9_-]+\.(png|webm)', p.name) and not p.is_symlink()
            and p.is_file() and 0 < p.stat().st_size <= MEDIA_LIMIT][:100]


def capture_read(name):
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.(png|webm)', name):
        raise ValueError('Invalid capture name.')
    directory = os.open(capture_directory(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MEDIA_LIMIT:
                raise ValueError('Invalid capture size or type.')
            raw = stream.read(MEDIA_LIMIT + 1)
            if len(raw) > MEDIA_LIMIT:
                raise ValueError('Capture too large.')
            return {'data': base64.b64encode(raw).decode()}
    finally:
        os.close(directory)


def valid_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use an HTTP(S) URL without embedded credentials.')
    return url


class Computer:
    def __init__(self):
        self.page = self.context = self.browser = self.playwright = None
        self.display = None
        self.lock = asyncio.Lock()
        self.frame = ''
        self.frame_at = 0
        self.controller = ''
        self.lease_until = 0
        self.recording = None
        self.notice = ''

    async def open(self):
        if self.page and not self.page.is_closed():
            return
        if self.context:
            self.page = await self.context.new_page()
            self.page.set_default_timeout(20000)
            return
        # Old filesystem snapshots keep their original OS packages. Upgrade
        # capture dependencies only when Computer is first used on one.
        if not shutil.which('Xvfb') or not shutil.which('ffmpeg'):
            self.notice = 'Preparing browser capture for this saved workspace…'
            for command in [('apt-get', 'update', '-qq'), ('apt-get', 'install', '-y', '-qq', 'xvfb', 'ffmpeg')]:
                proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.DEVNULL)
                if await asyncio.wait_for(proc.wait(), 240) != 0:
                    raise RuntimeError('Browser capture dependencies could not be prepared. Try again.')
        if not self.display or self.display.poll() is not None:
            self.display = subprocess.Popen(['Xvfb', ':99', '-screen', '0', f'{WIDTH}x{HEIGHT}x24', '-ac', '-nolisten', 'tcp'],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if Path('/tmp/.X11-unix/X99').exists():
                    break
                await asyncio.sleep(.1)
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=False,
            env={**os.environ, 'DISPLAY': ':99'}, args=['--no-sandbox', '--kiosk', '--window-position=0,0',
                                                     f'--window-size={WIDTH},{HEIGHT}'])
        self.context = await self.browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT})
        self.page = await self.context.new_page()
        self.page.set_default_timeout(20000)
        self.context.on('page', self.new_page)
        self.notice = ''
        await self.refresh_frame()

    def new_page(self, page):
        self.page = page
        page.set_default_timeout(20000)

    def controls(self):
        if self.lease_until < time.monotonic():
            self.controller = ''
        return self.controller

    async def refresh_frame(self):
        if not self.page or self.page.is_closed():
            return
        try:
            self.frame = base64.b64encode(await self.page.screenshot(type='jpeg', quality=65, timeout=3000)).decode()
            self.frame_at = time.time()
        except Exception:
            pass  # Keep the previous frame while a navigation is committing.

    async def frames(self):
        while True:
            if self.page:
                await self.refresh_frame()
            if self.recording and self.recording['process'].returncode is not None:
                async with self.lock:
                    try:
                        await self.stop_recording()
                        self.notice = 'Recording saved automatically at its time or size limit.'
                    except RuntimeError as exc:
                        self.notice = str(exc)
            await asyncio.sleep(1)

    def media(self):
        return capture_list()

    def budget(self):
        directory = capture_directory()
        used = sum(p.stat().st_size for p in directory.iterdir() if p.is_file() and not p.is_symlink())
        available = MEDIA_TOTAL - used
        if available < 1024 * 1024:
            raise ValueError('This workspace has reached its 64 MB capture budget. Download and remove old captures first.')
        return available

    async def stop_recording(self):
        if not self.recording:
            return {'recording': False}
        record, self.recording = self.recording, None
        process = record['process']
        if process.returncode is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 12)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        path = record['path']
        if not path.exists() or path.stat().st_size < 100 or process.returncode not in {0, 255}:
            if path.exists():
                path.rename(path.with_suffix('.partial'))
            raise RuntimeError('Recording did not finalize. Any partial capture is preserved in the workspace.')
        if path.stat().st_size > MEDIA_LIMIT:
            raise RuntimeError('Recording exceeded the file size limit.')
        final = path.with_suffix('.webm')
        path.replace(final)
        return {'path': str(final), 'name': final.name, 'recording': False, 'duration_seconds': round(time.time()-record['started'], 1)}

    async def state(self):
        return {'available': bool(self.page and not self.page.is_closed()), 'width': WIDTH, 'height': HEIGHT,
                'frame': self.frame if self.page and not self.page.is_closed() else '', 'frame_at': self.frame_at, 'url': self.page.url if self.page else '',
                'controller': self.controls(), 'recording': bool(self.recording),
                'recording_name': self.recording['path'].name if self.recording else '',
                'media': self.media(), 'notice': self.notice}

    async def command(self, body):
        action, actor = body.get('action'), body.get('actor', 'agent')
        if action == 'state':
            return await self.state()
        if action == 'release':
            if self.controls() == actor:
                self.controller = ''
            return await self.state()
        if action == 'claim':
            async with self.lock:
                if actor == 'agent':
                    raise ValueError('Only a signed-in person can take control.')
                if self.controls() and self.controller != actor:
                    raise ValueError('Another person is controlling this browser.')
                self.controller, self.lease_until = actor, time.monotonic()+60
                return await self.state()
        # Finish capture on every agent checkpoint/response before the archive.
        if action == 'finish':
            async with self.lock:
                return await self.stop_recording()
        async with self.lock:
            if self.controls() and self.controller != actor:
                raise ValueError('A person has control of the browser. Wait for them to release it; do not loop or retry browser actions.')
            if actor != 'agent' and self.controls() != actor:
                raise ValueError('Take control before interacting with the browser.')
            if actor != 'agent' and self.controller == actor:
                self.lease_until = time.monotonic()+60
            await self.open()
            args = body.get('args', {})
            if action == 'open':
                if args.get('url'):
                    await self.page.goto(valid_url(args['url']), wait_until='domcontentloaded')
            elif action == 'click':
                if 'role' in args:
                    await self.page.get_by_role(args['role'], name=args['name'], exact=True).click()
                else:
                    await self.page.mouse.click(max(0, min(WIDTH-1, float(args['x']))), max(0, min(HEIGHT-1, float(args['y']))))
            elif action == 'fill':
                await self.page.get_by_label(args['label'], exact=True).fill(args['value'])
            elif action == 'type':
                await self.page.keyboard.insert_text(str(args['text'])[:10000])
            elif action == 'key':
                key = args['key']
                if key not in {'Enter', 'Tab', 'Shift+Tab', 'Escape', 'Backspace', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Control+a', 'Meta+a', 'Space'}:
                    raise ValueError('Unsupported browser key.')
                await self.page.keyboard.press(key)
            elif action == 'scroll':
                await self.page.mouse.wheel(0, max(-1800, min(1800, int(args['dy']))))
            elif action == 'back':
                await self.page.go_back(wait_until='domcontentloaded')
            elif action == 'screenshot':
                available = self.budget()
                path = CAPTURES / capture_name(args.get('name'), 'png')
                raw = await self.page.screenshot(full_page=False)
                if len(raw) > min(MEDIA_LIMIT, available):
                    raise ValueError('Screenshot exceeds the capture budget.')
                partial = path.with_suffix('.partial')
                with partial.open('xb') as stream:
                    stream.write(raw)
                partial.replace(path)
                return {'path': str(path), 'name': path.name}
            elif action == 'record_start':
                if self.recording:
                    return {'recording': True, 'name': self.recording['path'].with_suffix('.webm').name}
                available = self.budget()
                path = CAPTURES / capture_name(args.get('name', 'flow'), 'partial')
                process = await asyncio.create_subprocess_exec('ffmpeg', '-y', '-loglevel', 'error', '-f', 'x11grab',
                    '-video_size', f'{WIDTH}x{HEIGHT}', '-framerate', '15', '-i', ':99.0', '-an',
                    '-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '6', '-b:v', '700k',
                    '-t', '600', '-fs', str(min(25*1024*1024, available-512*1024)), '-f', 'webm', str(path),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                self.recording = {'process': process, 'path': path, 'started': time.time()}
                await asyncio.sleep(.3)
                if process.returncode is not None:
                    result = await self.stop_recording()
                    return result
                return {'recording': True, 'name': path.with_suffix('.webm').name, 'path': str(path.with_suffix('.webm')), 'max_seconds': 600}
            elif action == 'record_stop':
                return await self.stop_recording()
            elif action != 'read':
                raise ValueError('Unknown computer action.')
            await self.refresh_frame()
            Path('/artifacts').mkdir(exist_ok=True)
            await self.page.screenshot(path='/artifacts/browser.png')
            return {'url': self.page.url, 'title': await self.page.title(),
                    'text': (await self.page.locator('body').inner_text())[:24000],
                    'elements': await self.page.locator('a,button,input,select,textarea').evaluate_all(
                        "els => els.slice(0,80).map(e => ({tag:e.tagName,role:e.getAttribute('role'),name:e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder'),type:e.type}))"),
                    'screenshot': 'The Computer panel shows the browser. Use browser_screenshot for a named saved capture.'}


async def serve():
    computer = Computer()
    async def handle(reader, writer):
        try:
            headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            if not headers.startswith(b'POST /command HTTP/'):
                raise ValueError('Invalid request')
            length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            if not 0 < length <= 65536:
                raise ValueError('Request too large')
            result = await computer.command(json.loads(await reader.readexactly(length)))
            status = 200
        except Exception as exc:
            status = 400
            result = {'error': str(exc)[:300] if isinstance(exc, (ValueError, RuntimeError)) else 'Browser action did not complete. Check the screen before retrying.'}
        data = json.dumps(result).encode()
        writer.write(f'HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode()+data)
        try:
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
    server = await asyncio.start_server(handle, '127.0.0.1', PORT, limit=65536)
    frames = asyncio.create_task(computer.frames())
    async with server:
        try:
            await server.serve_forever()
        finally:
            frames.cancel()
            await computer.stop_recording()


def request(body, *, start=True):
    def send():
        req = urllib.request.Request(f'http://127.0.0.1:{PORT}/command', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=520) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            return json.load(exc)
    try:
        return send()
    except urllib.error.URLError:
        if not start:
            return {'available': False}
    Path('/tmp/moyai-computer').mkdir(exist_ok=True)
    with open('/tmp/moyai-computer/start.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return send()
        except urllib.error.URLError:
            subprocess.Popen(['/usr/local/bin/python', __file__, 'serve'], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                time.sleep(.1)
                try:
                    return send()
                except urllib.error.URLError:
                    pass
    raise RuntimeError('Computer service could not start.')


if __name__ == '__main__':
    if sys.argv[1] == 'serve':
        asyncio.run(serve())
    elif sys.argv[1] == 'captures':
        print(json.dumps(capture_list()))
    elif sys.argv[1] == 'capture':
        print(json.dumps(capture_read(sys.argv[2])))
    else:
        body = json.loads(sys.argv[2])
        print(json.dumps(request(body, start=body.get('action') not in {'state', 'finish', 'release'})))
