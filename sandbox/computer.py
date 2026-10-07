"""One loopback-only browser per sandbox, shared by MCP and the Computer panel.

The control plane uses Modal exec to reach this service; no public port, browser
debug endpoint, gateway credential or desktop token is exposed to the web UI.
"""
import asyncio
import base64
import fcntl
import json
import io
import math
import os
from pathlib import Path
import re
import select
import shutil
import stat
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse
from uuid import uuid4

PORT = 18765
DISPLAY_SOCKET = Path('/tmp/.X11-unix/X99')
DISPLAY_LOCK = Path('/tmp/.X99-lock')
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


def display_alive():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(.2)
            probe.connect(str(DISPLAY_SOCKET))
        return True
    except OSError:
        return False


KEYS = {'Enter': 'Return', 'Tab': 'Tab', 'Escape': 'Escape', 'Backspace': 'BackSpace',
        'Delete': 'Delete', 'Insert': 'Insert', 'ArrowUp': 'Up', 'ArrowDown': 'Down', 'ArrowLeft': 'Left',
        'ArrowRight': 'Right', 'Home': 'Home', 'End': 'End', 'PageUp': 'Prior',
        'PageDown': 'Next', 'Space': 'space', '+': 'plus', '-': 'minus', '=': 'equal',
        '/': 'slash', '?': 'question', ',': 'comma', '.': 'period', '[': 'bracketleft', ']': 'bracketright', **{f'F{i}': f'F{i}' for i in range(1, 13)}}


def input_commands(events):
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ValueError('Use 1–100 desktop input events.')
    commands = []
    text_size = 0
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('Invalid desktop input event.')
        kind = event.get('type')
        if kind in {'text', 'paste'}:
            value = event.get('text')
            if not isinstance(value, str) or not value or '\0' in value:
                raise ValueError('Invalid desktop text.')
            text_size += len(value)
            if text_size > 10000:
                raise ValueError('Desktop text is too long.')
            # X11 remaps missing Unicode keysyms; give Chromium time to read each mapping.
            commands.append((['paste'] if kind == 'paste' else
                             ['type', '--clearmodifiers', '--delay', '12' if not value.isascii() else '0', '--file', '-'], value))
        elif kind == 'key':
            key = event.get('key')
            if not isinstance(key, str):
                raise ValueError('Invalid desktop key.')
            parts = key[:-1].split('+')[:-1]+['+'] if key.endswith('++') else key.split('+')
            if any(part not in {'Control', 'Alt', 'Shift', 'Meta'} for part in parts[:-1]):
                raise ValueError('Unsupported desktop shortcut.')
            base = KEYS.get(parts[-1], parts[-1] if re.fullmatch(r'[a-zA-Z0-9]', parts[-1]) else '')
            if not base:
                raise ValueError('Unsupported desktop key.')
            modifiers = ['ctrl' if part in {'Control', 'Meta'} else part.lower() for part in parts[:-1]]
            commands.append((['key', '--clearmodifiers', '+'.join([*modifiers, base])], None))
        elif kind == 'pointer':
            phase, button = event.get('phase'), event.get('button', 0)
            if phase not in {'move', 'down', 'up'} or type(button) is not int or button not in range(3):
                raise ValueError('Invalid desktop pointer.')
            coordinates = []
            if phase != 'up' or 'x' in event or 'y' in event:
                x, y = event.get('x'), event.get('y')
                if any(type(value) not in {int, float} or not math.isfinite(value) for value in (x, y)):
                    raise ValueError('Invalid desktop coordinates.')
                coordinates = ['mousemove', str(round(max(0, min(WIDTH-1, x)))), str(round(max(0, min(HEIGHT-1, y))))]
            mouse_button = str({0: 1, 1: 2, 2: 3}[button])
            arguments = coordinates + ([] if phase == 'move' else ['mousedown' if phase == 'down' else 'mouseup', mouse_button])
            commands.append((arguments, None))
        elif kind == 'scroll':
            for name, negative, positive in [('dy', '4', '5'), ('dx', '6', '7')]:
                value = event.get(name, 0)
                if type(value) not in {int, float} or not math.isfinite(value):
                    raise ValueError('Invalid desktop scroll.')
                if value:
                    commands.append((['click', '--repeat', str(min(15, max(1, round(abs(value)/80)))), '--delay', '0',
                                      negative if value < 0 else positive], None))
        else:
            raise ValueError('Unknown desktop input event.')
    return commands


class Computer:
    def __init__(self):
        self.page = self.context = self.browser = self.playwright = None
        self.display = None
        self.desktop = None
        self.panel = None
        self.clipboard = None
        self.lock = asyncio.Lock()
        self.frame = ''
        self.frame_at = 0
        self.controller = ''
        self.lease_until = 0
        self.release_pending = False
        self.recording = None
        self.notice = ''

    async def ensure_desktop(self):
        # Restored snapshots have refreshed Python but retain old OS packages.
        packages = {'Xvfb': 'xvfb', 'ffmpeg': 'ffmpeg', 'openbox': 'openbox',
                    'tint2': 'tint2', 'xterm': 'xterm', 'xdotool': 'xdotool',
                    'xclip': 'xclip', 'xsetroot': 'x11-xserver-utils'}
        missing = [package for executable, package in packages.items() if not shutil.which(executable)]
        if missing:
            self.notice = 'Preparing the desktop for this saved workspace…'
            for command in [('apt-get', 'update', '-qq'),
                            ('apt-get', 'install', '-y', '-qq', '--no-install-recommends', *missing)]:
                proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.DEVNULL)
                if await asyncio.wait_for(proc.wait(), 90 if command[1] == 'update' else 240) != 0:
                    raise RuntimeError('Desktop dependencies could not be prepared. Try again.')
        try:
            from PIL import ImageGrab
        except ImportError:
            proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pip', 'install', 'pillow==12.3.0',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await asyncio.wait_for(proc.wait(), 90) != 0:
                raise RuntimeError('Desktop capture could not be prepared. Try again.')
        await self.ensure_display()
        env = {**os.environ, 'DISPLAY': ':99'}
        if not self.desktop or self.desktop.poll() is not None:
            self.desktop = subprocess.Popen(['openbox', '--sm-disable'], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['xsetroot', '-solid', '#eeedf4'], env=env, check=True, timeout=5)
        if not self.panel or self.panel.poll() is not None:
            self.panel = subprocess.Popen(['tint2'], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.notice = ''

    async def open(self):
        await self.ensure_desktop()
        if self.browser and not self.browser.is_connected():
            self.page = self.context = self.browser = None
        if self.context:
            await self.active_page()
            if self.page:
                return
            self.page = await self.context.new_page()
            self.page.set_default_timeout(20000)
            await self.page.goto('chrome://newtab/')
            return
        from playwright.async_api import async_playwright
        if not self.playwright:
            self.playwright = await async_playwright().start()
        if not self.browser:
            self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=False,
                env={**os.environ, 'DISPLAY': ':99'}, args=['--no-sandbox', '--start-maximized', '--window-position=0,0',
                                                         f'--window-size={WIDTH},{HEIGHT}'])
        self.context = await self.browser.new_context(no_viewport=True)
        self.context.on('page', self.new_page)
        self.context.on('close', self.context_closed)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(20000)
        await self.page.goto('chrome://newtab/')
        await self.refresh_frame()

    def context_closed(self, context):
        if self.context is context:
            self.context = self.page = None

    async def active_page(self):
        """Follow the tab/window the person selected before semantic agent tools."""
        if not self.context:
            return
        pages = [page for page in self.context.pages if not page.is_closed()]
        visible = []
        for page in pages:
            try:
                state = await asyncio.wait_for(page.evaluate(
                    '({visible: document.visibilityState === "visible", focused: document.hasFocus()})'), 1)
                if state['focused']:
                    self.page = page
                    return
                if state['visible']:
                    visible.append(page)
            except Exception:
                continue  # A navigating tab can be selected on the next read.
        self.page = next(iter(visible), self.page if self.page in pages else next(iter(pages), None))

    async def ensure_display(self):
        if not self.display or self.display.poll() is not None:
            # Filesystem snapshots preserve X lock/socket files, not the live
            # display process. Probe before reusing a socket or removing stale
            # files, otherwise a resumed workspace can never open Chromium.
            if not display_alive():
                DISPLAY_LOCK.unlink(missing_ok=True)
                DISPLAY_SOCKET.unlink(missing_ok=True)
                self.display = subprocess.Popen(['Xvfb', ':99', '-screen', '0', f'{WIDTH}x{HEIGHT}x24', '-ac', '-nolisten', 'tcp'],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(50):
                    if display_alive():
                        break
                    if self.display.poll() is not None:
                        raise RuntimeError('The browser display could not start. Try a new response to restore the workspace.')
                    await asyncio.sleep(.1)
                else:
                    raise RuntimeError('The browser display is still starting. Try again shortly.')

    def new_page(self, page):
        self.page = page
        page.set_default_timeout(20000)

    def controls(self):
        if self.controller and self.lease_until < time.monotonic():
            self.controller = ''
            self.release_pending = True
        return self.controller

    def desktop_image(self, image_format='JPEG'):
        from PIL import ImageGrab
        raw = io.BytesIO()
        ImageGrab.grab(xdisplay=':99').save(raw, format=image_format, **({'quality': 70} if image_format == 'JPEG' else {}))
        return raw.getvalue()

    async def refresh_frame(self):
        if not self.desktop:
            return
        try:
            self.frame = base64.b64encode(await asyncio.to_thread(self.desktop_image)).decode()
            self.frame_at = time.time()
        except Exception:
            self.notice = 'Desktop view is reconnecting…'

    async def frames(self):
        while True:
            async with self.lock:
                self.controls()
                if self.release_pending:
                    await self.release_buttons()
            if self.desktop:
                await self.refresh_frame()
            if self.recording and self.recording['process'].returncode is not None:
                async with self.lock:
                    try:
                        await self.stop_recording()
                        self.notice = 'Recording saved automatically at its time or size limit.'
                    except RuntimeError as exc:
                        self.notice = str(exc)
            await asyncio.sleep(.1)

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
        return {'available': bool(self.desktop and self.desktop.poll() is None), 'surface': 'desktop',
                'width': WIDTH, 'height': HEIGHT, 'frame': self.frame, 'frame_at': self.frame_at,
                'url': self.page.url if self.page and not self.page.is_closed() else '',
                'controller': self.controls(), 'recording': bool(self.recording),
                'recording_name': self.recording['path'].name if self.recording else '',
                'media': self.media(), 'notice': self.notice}

    async def desktop_input(self, events):
        # Validate the entire batch before any event takes effect. Never run a shell.
        commands = input_commands(events)
        for arguments, text in commands:
            if arguments == ['paste']:
                await self.paste(text)
                continue
            proc = await asyncio.create_subprocess_exec('xdotool', *arguments, env={**os.environ, 'DISPLAY': ':99'},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.communicate(text.encode() if text is not None else None),
                                       10 + (len(text)*.024 if text and not text.isascii() else 0))
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError('Desktop input timed out. Check the screen before continuing.') from None
            if proc.returncode:
                raise RuntimeError('Desktop input did not complete. Check the screen before continuing.')

    async def clear_clipboard(self):
        process, self.clipboard = self.clipboard, None
        if process and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def paste(self, text):
        # Clipboard requests include format discovery before the actual data.
        # Keep the owner alive until replaced or control ends, like a desktop
        # clipboard; never count TARGETS queries as completed text transfers.
        await self.clear_clipboard()
        process = await asyncio.create_subprocess_exec('xclip', '-selection', 'clipboard', '-in', '-quiet',
            env={**os.environ, 'DISPLAY': ':99', 'LC_ALL': 'C'}, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        self.clipboard = process
        try:
            process.stdin.write(text.encode())
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
            await asyncio.wait_for(process.stderr.readuntil(b'  Waiting for selection request number 1\n'), 5)
            await self.desktop_input([{'type': 'key', 'key': 'Control+v'}])
        except BaseException:
            await self.clear_clipboard()
            raise

    async def release_buttons(self):
        self.release_pending = False
        await self.clear_clipboard()
        if self.desktop:
            await self.desktop_input([{'type': 'pointer', 'phase': 'up', 'button': button} for button in range(3)])

    async def command(self, body):
        action, actor = body.get('action'), body.get('actor', 'agent')
        if action == 'state':
            return await self.state()
        if action == 'release':
            async with self.lock:
                if self.controls() == actor:
                    await self.release_buttons()
                    self.controller = ''
                return await self.state()
        if action == 'claim':
            async with self.lock:
                if actor == 'agent':
                    raise ValueError('Only a signed-in person can take control.')
                if self.controls() and self.controller != actor:
                    raise ValueError('Another person is controlling this browser.')
                if self.controller != actor:
                    await self.release_buttons()
                self.controller, self.lease_until = actor, time.monotonic()+60
                return await self.state()
        # Finish capture on every agent checkpoint/response before the archive.
        if action == 'finish':
            async with self.lock:
                return await self.stop_recording()
        async with self.lock:
            controller = self.controls()
            if self.release_pending:
                await self.release_buttons()
            if controller and self.controller != actor:
                raise ValueError('A person has control of the browser. Wait for them to release it; do not loop or retry browser actions.')
            if actor != 'agent' and self.controls() != actor:
                raise ValueError('Take control before interacting with the browser.')
            if actor != 'agent' and self.controller == actor:
                self.lease_until = time.monotonic()+60
            args = body.get('args', {})
            if action == 'input':
                if actor == 'agent':
                    raise ValueError('Desktop input requires a signed-in controller.')
                input_commands(args.get('events'))
                await self.ensure_desktop()
                await self.desktop_input(args['events'])
                self.lease_until = time.monotonic()+60
                await self.refresh_frame()
                return await self.state()
            await self.open()
            if actor != 'agent':
                self.lease_until = time.monotonic()+60
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
                raw = await asyncio.to_thread(self.desktop_image, 'PNG')
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
            if actor != 'agent':
                return await self.state()
            Path('/artifacts').mkdir(exist_ok=True)
            await self.page.screenshot(path='/artifacts/browser.png')
            return {'url': self.page.url, 'title': await self.page.title(),
                    'text': (await self.page.locator('body').inner_text())[:24000],
                    'elements': await self.page.locator('a,button,input,select,textarea').evaluate_all(
                        "els => els.slice(0,80).map(e => ({tag:e.tagName,role:e.getAttribute('role'),name:e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder'),type:e.type}))"),
                    'screenshot': 'The Computer panel shows the browser. Use browser_screenshot for a named saved capture.'}


async def serve(desktop=None):
    computer = desktop or Computer()
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
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ConnectionRefusedError):
            raise  # A lost reply does not prove the command was not applied.
        if not start:
            return {'available': False}
    Path('/tmp/moyai-computer').mkdir(exist_ok=True)
    with open('/tmp/moyai-computer/start.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return send()
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, ConnectionRefusedError):
                raise
            subprocess.Popen(['/usr/local/bin/python', __file__, 'serve'], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                time.sleep(.1)
                try:
                    return send()
                except urllib.error.URLError as exc:
                    if not isinstance(exc.reason, ConnectionRefusedError):
                        raise
    raise RuntimeError('Computer service could not start.')


def bridge():
    # One authenticated private exec carries many commands. No input is logged.
    # The app retires idle handles after 20s, before this 30s idle expiry.
    while select.select([sys.stdin], [], [], 30)[0]:
        line = sys.stdin.buffer.readline(65537)
        if not line or len(line) > 65536:
            return
        try:
            body = json.loads(line)
            result = request(body, start=body.get('action') not in {'state', 'finish', 'release'})
        except Exception:
            # Lost/invalid responses end the channel; the caller stops input.
            return
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'serve':
        asyncio.run(serve())
    elif sys.argv[1] == 'bridge':
        bridge()
    elif sys.argv[1] == 'captures':
        print(json.dumps(capture_list()))
    elif sys.argv[1] == 'capture':
        print(json.dumps(capture_read(sys.argv[2])))
    else:
        body = json.loads(sys.argv[2])
        print(json.dumps(request(body, start=body.get('action') not in {'state', 'finish', 'release'})))
