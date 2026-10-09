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
MAX_BROWSER_STATE = 2 * 1024 * 1024 - 65536
PRIVATE_BODY_LIMIT = MAX_BROWSER_STATE + 65536
BROWSER_ACTIVE_TIMEOUT = 1
BROWSER_STORAGE_TIMEOUT = 5
BROWSER_PAGE_TIMEOUT = 1
BROWSER_CLOSE_TIMEOUT = 3


def starts_service(body):
    return body.get('action') not in {'state', 'finish', 'release'} or body.get('args', {}).get('browser') == 'restore'


def browser_state(value):
    """Validate the private checkpoint without ever including its contents in errors."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'storage', 'pages', 'active'}
            or not isinstance(value['storage'], dict) or not isinstance(value['pages'], list)
            or len(value['pages']) > 32 or type(value['active']) is not int
            or not 0 <= value['active'] < max(1, len(value['pages']))
            or len(json.dumps(value, allow_nan=False).encode()) > MAX_BROWSER_STATE):
        raise ValueError('Invalid browser checkpoint.')
    for page in value['pages']:
        if (not isinstance(page, dict) or set(page) != {'url', 'session_storage'}
                or not isinstance(page['url'], str) or not isinstance(page['session_storage'], dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in page['session_storage'].items())):
            raise ValueError('Invalid browser checkpoint.')
        valid_url(page['url'])
    return value


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


def valid_tab(tab):
    if not isinstance(tab, str) or len(tab) > 512 or not re.fullmatch(r'(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*|)', tab):
        raise ValueError('Use a canonical GitHub pull request URL for this browser tab.')
    return tab
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
        self.review_context = None
        self.review_pages = {}
        self.frame_cache = {}
        self.foreground_tab = ''
        self.return_window = self.foreground_window = None
        self.review_window_states = {}
        self.controller_tab = ''
        self.release_tab = ''
        self.frame = ''
        self.frame_at = 0
        self.controller = ''
        self.lease_until = 0
        self.release_pending = False
        self.recording = None
        self.stopping = False
        self.notice = ''
        self.browser_scope = ''
        self.browser_admitted = False
        self.saved_browser = None
        self.saved_pages = {}
        self.browser_partial = False
        self.browser_blocked = False

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
            subprocess.run(['xsetroot', '-solid', '#eeedf4'], env=env, check=True, timeout=30)
        if not self.panel or self.panel.poll() is not None:
            self.panel = subprocess.Popen(['tint2'], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.notice = ''

    async def open(self):
        if self.browser_blocked:
            raise ValueError('Saved browser access is unavailable. The checkpoint is preserved; non-browser work can continue.')
        await self.start_browser()
        if self.context:
            await self.active_page()
            if self.page:
                return
        else:
            saved = self.saved_browser
            self.context = await self.browser.new_context(no_viewport=True,
                **({'storage_state': saved['storage']} if saved else {}))
            self.context.on('page', self.new_page)
            self.context.on('close', self.context_closed)
            if saved and saved['pages']:
                context = self.context
                try:
                    self.page = await self.restore_pages(context, saved)
                    await self.page.bring_to_front()
                    await self.refresh_frame()
                except BaseException:
                    self.context = self.page = None
                    self.saved_pages.clear()
                    try:
                        await asyncio.wait_for(context.close(), BROWSER_CLOSE_TIMEOUT)
                    except Exception:
                        pass  # Preserve the checkpoint and retry in a fresh context.
                    raise
                return
        self.page = await self.context.new_page()
        self.page.set_default_timeout(20000)
        await self.page.goto('chrome://newtab/')
        await self.refresh_frame()

    async def restore_pages(self, context, saved):
        pages = []
        for entry in saved['pages']:
            page = await context.new_page()
            pages.append(page)
            # Remove this origin-bound initializer after initial navigation so
            # a later logout or navigation cannot resurrect an old token.
            session = await context.new_cdp_session(page)
            await session.send('Page.enable')
            script = await session.send('Page.addScriptToEvaluateOnNewDocument', {'source': '''(() => {
                const saved = %s;
                if (location.origin === new URL(saved.url).origin) {
                    for (const [key, value] of Object.entries(saved.session_storage)) sessionStorage.setItem(key, value);
                }
            })();''' % json.dumps(entry)})
            try:
                await page.goto(entry['url'], wait_until='domcontentloaded')
            except Exception:
                self.notice = 'A saved browser tab could not reconnect. You can reload it.'
            finally:
                if not page.is_closed():
                    await session.send('Page.removeScriptToEvaluateOnNewDocument', {'identifier': script['identifier']})
                    await session.detach()
                    self.saved_pages[page] = entry
        # Site-created popups and closed pages cannot change saved tab identity.
        page = next((p for p in [pages[saved['active']], *pages, *context.pages] if not p.is_closed()), None)
        if page is None:
            page = await context.new_page()
            await page.goto('chrome://newtab/')
        return page

    async def start_browser(self):
        await self.ensure_desktop()
        if self.browser and not self.browser.is_connected():
            self.page = self.context = self.browser = self.review_context = None
            self.saved_pages.clear()
            self.review_pages.clear()
            self.frame_cache.clear()
        if self.browser:
            return
        from playwright.async_api import async_playwright
        if not self.playwright:
            self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=False,
            env={**os.environ, 'DISPLAY': ':99'}, args=['--no-sandbox', '--start-maximized', '--window-position=0,0',
                                                     f'--window-size={WIDTH},{HEIGHT}'])

    def context_closed(self, context):
        if self.context is context:
            self.context = self.page = None
            self.saved_pages.clear()

    async def checkpoint_page(self, page):
        try:
            entry = await asyncio.wait_for(page.evaluate(
                '({url: location.href, session_storage: Object.fromEntries(Object.entries(sessionStorage))})'), BROWSER_PAGE_TIMEOUT)
            return entry, False
        except Exception:
            previous = self.saved_pages.get(page)
            # URL alone is not tab identity, and navigation invalidates old tokens.
            entry = previous if previous and previous['url'] == page.url else {'url': page.url, 'session_storage': {}}
            return entry, True

    async def checkpoint(self):
        if self.browser_blocked:
            raise ValueError('Saved browser access is unavailable. The checkpoint is preserved.')
        if not self.context:
            return self.saved_browser
        try:
            await asyncio.wait_for(self.active_page(), BROWSER_ACTIVE_TIMEOUT)
        except TimeoutError:
            pass  # The last tracked active page remains a valid selection.
        storage = await asyncio.wait_for(self.context.storage_state(indexed_db=True), BROWSER_STORAGE_TIMEOUT)
        pages = [page for page in self.context.pages if not page.is_closed() and urlparse(page.url).scheme in {'http', 'https'}]
        selected = pages[:31] + [self.page] if self.page in pages[32:] else pages[:32]
        entries = await asyncio.gather(*(self.checkpoint_page(page) for page in selected))
        saved = {page: entry for page, (entry, _) in zip(selected, entries) if not page.is_closed()}
        active = list(saved).index(self.page) if self.page in saved else 0
        self.saved_browser = browser_state({'storage': storage, 'pages': list(saved.values()), 'active': active})
        self.saved_pages = saved
        self.browser_partial = len(pages) > 32 or any(partial for _, partial in entries)
        return self.saved_browser

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
        # Cold image pages can take longer than five seconds to reach Xvfb.
        # An existing process may also still be starting after an earlier
        # request timed out; always wait for the socket before using it.
        for _ in range(300):
            if display_alive():
                return
            if self.display and self.display.poll() is not None:
                raise RuntimeError('The browser display could not start. Try a new response to restore the workspace.')
            await asyncio.sleep(.1)
        raise RuntimeError('The browser display is still starting. Try again shortly.')

    def new_page(self, page):
        self.page = page
        page.set_default_timeout(20000)

    def page_for(self, tab=''):
        if not tab:
            return self.page if self.page and not self.page.is_closed() else None
        pages = [p for p in self.review_pages.get(tab, []) if not p.is_closed()]
        if tab in self.review_pages:
            self.review_pages[tab] = pages
        return pages[-1] if pages else None

    def review_page_count(self):
        return sum(not page.is_closed() for pages in self.review_pages.values() for page in pages)

    def track_review_page(self, tab, page):
        self.review_pages.setdefault(tab, []).append(page)
        page.set_default_timeout(20000)
        async def popup(child):
            async with self.lock:
                if self.stopping or self.review_page_count() >= 16 or page.is_closed():
                    await child.close()
                    return
                self.track_review_page(tab, child)
                self.frame_cache.pop(tab, None)
                try:
                    owner = self.page_for(self.foreground_tab) if self.owns_foreground() else None
                    if owner:
                        await self.show_review_page(owner)
                    else:
                        await self.review_windows(minimize=True, page=child)
                        await self.settle_foreground()
                except Exception:
                    self.notice = 'Desktop handoff is reconnecting. Check the screen before continuing.'
        page.on('popup', popup)

    async def open_review_page(self, tab):
        if self.review_page_count() >= 16:
            raise ValueError('Close a pull request tab before opening another (16-tab limit).')
        await self.start_browser()
        await self.begin_review_foreground(tab)
        if not self.review_context:
            self.review_context = await self.browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT})
        page = await self.review_context.new_page()
        self.track_review_page(tab, page)
        try:
            await page.goto(tab, wait_until='domcontentloaded')
        except Exception:
            await page.close()
            raise
        return page

    async def window_command(self, *args):
        if not self.desktop:
            return None
        proc = await asyncio.create_subprocess_exec('xdotool', *args, env={**os.environ, 'DISPLAY': ':99'},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 3)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        return stdout.decode().strip() if proc.returncode == 0 else None

    async def review_windows(self, *, minimize, page=None):
        if not self.desktop or not self.review_context:
            return
        async with asyncio.timeout(5):
            seen = set()
            for target in [page] if page else list(self.review_context.pages):
                if target.is_closed():
                    continue
                session = None
                try:
                    session = await self.review_context.new_cdp_session(target)
                    window = await session.send('Browser.getWindowForTarget')
                    identity, current = window['windowId'], window['bounds']['windowState']
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if minimize and current != 'minimized':
                        self.review_window_states.setdefault(identity, current)
                    desired = 'minimized' if minimize else self.review_window_states.get(identity, 'normal')
                    if (minimize or identity in self.review_window_states) and current != desired:
                        if current != 'normal':
                            await session.send('Browser.setWindowBounds', {'windowId': identity, 'bounds': {'windowState': 'normal'}})
                        if desired != 'normal':
                            await session.send('Browser.setWindowBounds', {'windowId': identity, 'bounds': {'windowState': desired}})
                    if not minimize:
                        self.review_window_states.pop(identity, None)
                except Exception:
                    if not target.is_closed():
                        raise
                finally:
                    if session:
                        try:
                            await asyncio.wait_for(session.detach(), 1)
                        except Exception:
                            pass
            if page is None:
                for identity in set(self.review_window_states) - seen:
                    self.review_window_states.pop(identity, None)

    async def begin_review_foreground(self, tab):
        if not self.foreground_tab:
            self.return_window = await self.window_command('getactivewindow')
        self.foreground_tab = tab

    async def show_review_page(self, page):
        await self.review_windows(minimize=False, page=page)
        await page.bring_to_front()
        self.foreground_window = await self.window_command('getactivewindow')

    def owns_foreground(self):
        tab = self.foreground_tab
        return bool(tab and ((self.recording and self.recording.get('tab', '') == tab)
                    or (self.controls() and self.controller_tab == tab and self.page_for(tab))))

    async def settle_foreground(self):
        if self.stopping or not self.foreground_tab or self.owns_foreground():
            return
        current = await self.window_command('getactivewindow')
        await self.review_windows(minimize=True)
        if self.return_window and current == self.foreground_window:
            # A vanished native window is a safe fallback to the revealed desktop.
            await self.window_command('windowactivate', '--sync', self.return_window)
        self.foreground_tab = ''
        self.return_window = self.foreground_window = None
        await self.refresh_frame()

    def controls(self):
        if self.controller and self.lease_until < time.monotonic():
            self.release_tab = self.controller_tab
            self.controller = self.controller_tab = ''
            self.release_pending = True
        return self.controller

    def desktop_image(self, image_format='JPEG'):
        from PIL import ImageGrab
        raw = io.BytesIO()
        ImageGrab.grab(xdisplay=':99').save(raw, format=image_format, **({'quality': 70} if image_format == 'JPEG' else {}))
        return raw.getvalue()

    async def refresh_frame(self, tab=''):
        if not tab:
            if not self.desktop:
                return
            try:
                self.frame = base64.b64encode(await asyncio.to_thread(self.desktop_image)).decode()
                self.frame_at = time.time()
            except Exception:
                self.notice = 'Desktop view is reconnecting…'
            return
        page = self.page_for(tab)
        if not page:
            self.frame_cache.pop(tab, None)
            return
        try:
            frame = base64.b64encode(await page.screenshot(type='jpeg', quality=65, timeout=3000)).decode()
            if self.page_for(tab) is page:
                self.frame_cache[tab] = (frame, time.time())
        except Exception:
            pass  # Keep the previous frame while a navigation is committing.

    async def frames(self):
        while True:
            async with self.lock:
                try:
                    self.controls()
                    if self.release_pending:
                        await self.release_buttons()
                    await self.settle_foreground()
                except Exception:
                    self.notice = 'Desktop handoff is reconnecting. Check the screen before continuing.'
            if self.desktop:
                await self.refresh_frame()
            if self.foreground_tab:
                await self.refresh_frame(self.foreground_tab)
            if self.recording and self.recording['process'].returncode is not None:
                async with self.lock:
                    try:
                        await self.stop_recording()
                        self.notice = 'Recording saved automatically at its time or size limit.'
                    except RuntimeError as exc:
                        self.notice = str(exc)
            await asyncio.sleep(.05 if self.controls() else .1)

    def media(self):
        return capture_list()

    def budget(self):
        directory = capture_directory()
        used = sum(p.stat().st_size for p in directory.iterdir() if p.is_file() and not p.is_symlink())
        available = MEDIA_TOTAL - used
        if available < 1024 * 1024:
            raise ValueError('This workspace has reached its 64 MB capture budget. Download and remove old captures first.')
        return available

    def agent_recording(self):
        return bool(self.recording and not self.recording.get('tab', '')
                    and self.recording.get('actor', 'agent') == 'agent')

    async def stop_recording(self):
        if not self.recording:
            return {'recording': False}
        record, self.recording = self.recording, None
        try:
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
        finally:
            try:
                await self.settle_foreground()
            except Exception:
                self.notice = 'Desktop handoff is reconnecting. Check the screen before continuing.'

    async def state(self, tab='', *, include_frame=True):
        page = self.page_for(tab)
        if page and tab:
            await self.refresh_frame(tab)
        frame, stamp = self.frame_cache.get(tab, ('', 0)) if page else ('', 0)
        if not tab:
            frame, stamp = self.frame, self.frame_at
        available = bool(page) if tab else bool(self.desktop and self.desktop.poll() is None)
        controller = self.controls()
        recording = bool(self.recording and self.recording.get('tab', '') == tab)
        return {'available': available, 'surface': 'browser' if tab else 'desktop', 'tab': tab, 'width': WIDTH, 'height': HEIGHT,
                **({'frame': frame, 'frame_at': stamp} if include_frame else {}), 'url': page.url if page else '',
                'controller': controller, 'controller_tab': self.controller_tab, 'recording': recording,
                'recording_elsewhere': bool(self.recording and not recording),
                'recording_name': self.recording['path'].name if recording else '',
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
        tab = self.release_tab if self.release_pending else self.controller_tab
        if tab:
            page = self.page_for(tab)
            if page:
                for button in ('left', 'middle', 'right'):
                    await page.mouse.up(button=button)
        else:
            await self.clear_clipboard()
            if self.desktop:
                await self.desktop_input([{'type': 'pointer', 'phase': 'up', 'button': button} for button in range(3)])
        self.release_pending = False
        self.release_tab = ''

    async def browser_input(self, page, events):
        # The same batch validator admits both surfaces before any event applies.
        input_commands(events)
        for event in events:
            kind = event['type']
            if kind in {'text', 'paste'}:
                await page.keyboard.insert_text(event['text'])
            elif kind == 'key':
                await page.keyboard.press(event['key'].replace('Meta+', 'Control+'))
            elif kind == 'pointer':
                if event['phase'] != 'up' or 'x' in event or 'y' in event:
                    await page.mouse.move(round(max(0, min(WIDTH-1, event['x']))), round(max(0, min(HEIGHT-1, event['y']))))
                button = ('left', 'middle', 'right')[event.get('button', 0)]
                if event['phase'] == 'down':
                    await page.mouse.down(button=button)
                elif event['phase'] == 'up':
                    await page.mouse.up(button=button)
            elif kind == 'scroll':
                await page.mouse.wheel(max(-1200, min(1200, event.get('dx', 0))), max(-1200, min(1200, event.get('dy', 0))))

    async def command(self, body):
        action, actor = body.get('action'), body.get('actor', 'agent')
        if action == 'state' and body.get('args', {}).get('browser') in {'restore', 'checkpoint'}:
            action = body['args']['browser']
        tab = valid_tab(body.get('tab', ''))
        if tab and actor == 'agent':
            raise ValueError('Only a signed-in person can use pull request tabs.')
        if action == 'finish' and body.get('args', {}).get('all') is True:
            self.stopping = True
        if action == 'checkpoint' and body.get('args', {}).get('releasing') is True:
            self.stopping = True
        if self.stopping and action not in {'state', 'finish', 'checkpoint'}:
            raise ValueError('This workspace is shutting down. Start a new response to use Computer again.')
        if action == 'state':
            return await self.state(tab)
        async with self.lock:
            if action in {'restore', 'checkpoint'}:
                scope = body.get('args', {}).get('scope')
                if tab or not isinstance(scope, str) or not re.fullmatch(r'[a-f0-9]{32}', scope):
                    raise ValueError('Invalid browser checkpoint scope.')
                if self.browser_scope and self.browser_scope != scope:
                    raise ValueError('Browser checkpoint belongs to another session.')
                if action == 'restore':
                    if body['args'].get('blocked') is True:
                        self.browser_blocked = True
                        self.browser_scope = scope
                    elif not self.browser_admitted or self.browser_blocked:
                        saved = browser_state(body['args'].get('state'))
                        if not self.browser_admitted and self.context and saved is not None:
                            # An agent may have opened a fresh browser after an
                            # unknown restore outcome. Scope reservation alone
                            # cannot make that context authoritative for this run.
                            self.browser_scope, self.browser_blocked = scope, True
                            await asyncio.wait_for(self.context.close(), BROWSER_CLOSE_TIMEOUT)
                            self.context = self.page = None
                            self.saved_pages.clear()
                        if not self.context:
                            self.saved_browser = saved
                        self.browser_scope, self.browser_blocked = scope, False
                        self.browser_admitted = True
                    return {'scope': scope, 'restored': not self.browser_blocked}
                return {'scope': self.browser_scope, 'state': await self.checkpoint(), 'partial': self.browser_partial}
            if self.stopping and action != 'finish':
                raise ValueError('This workspace is shutting down. Start a new response to use Computer again.')
            self.controls()
            # Finalization cannot depend on a live control lease or successful handoff.
            if action == 'finish':
                if body.get('args', {}).get('all') is True or self.agent_recording():
                    return await self.stop_recording()
                return {'recording': bool(self.recording)}
            released = self.release_pending
            if released:
                await self.release_buttons()
            await self.settle_foreground()
            if action == 'wake':
                await self.ensure_desktop()
                await self.refresh_frame()
                return await self.state(tab)
            if action == 'release':
                if self.controls() == actor and self.controller_tab == tab:
                    await self.release_buttons()
                    self.controller = self.controller_tab = ''
                    await self.settle_foreground()
                return await self.state(tab)
            if action == 'claim':
                if actor == 'agent':
                    raise ValueError('Only a signed-in person can take control.')
                if self.controls() and self.controller != actor:
                    raise ValueError('Another person is controlling this browser.')
                if self.recording and self.recording.get('tab', '') != tab:
                    raise ValueError('Stop the recording in its current tab before switching control.')
                if not released and (self.controller != actor or self.controller_tab != tab):
                    await self.release_buttons()
                self.controller, self.controller_tab, self.lease_until = actor, tab, time.monotonic()+60
                await self.settle_foreground()
                return await self.state(tab)
            if action == 'close_tab':
                if not tab:
                    raise ValueError('Only a pull request tab can be closed.')
                controller = self.controls()
                if self.recording and self.recording.get('tab', '') == tab:
                    raise ValueError('Stop the recording before closing this tab.')
                if not self.page_for(tab):
                    self.review_pages.pop(tab, None)
                    self.frame_cache.pop(tab, None)
                    return await self.state(tab)
                if controller and controller != actor:
                    raise ValueError('Another person is controlling this browser.')
                if self.controller_tab == tab:
                    await self.release_buttons()
                for page in self.review_pages.get(tab, []):
                    if not page.is_closed():
                        await page.close()
                self.review_pages.pop(tab, None)
                self.frame_cache.pop(tab, None)
                if self.controller_tab == tab:
                    self.controller = self.controller_tab = ''
                await self.settle_foreground()
                return await self.state(tab)
            if self.controls() and self.controller != actor:
                raise ValueError('A person has control of the browser. Wait for them to release it; do not loop or retry browser actions.')
            if actor != 'agent' and (self.controls() != actor or self.controller_tab != tab):
                raise ValueError('Take control of this tab before interacting with the browser.')
            if self.recording and self.recording.get('tab', '') != tab:
                raise ValueError('Stop the recording in its current tab before switching control.')
            if actor != 'agent':
                self.lease_until = time.monotonic()+60
            if action == 'record_stop':
                if actor == 'agent' and self.recording and not self.agent_recording():
                    raise ValueError('A person is recording this browser. They must stop the recording.')
                return await self.stop_recording()
            args = body.get('args', {})
            if action == 'input':
                if actor == 'agent':
                    raise ValueError('Desktop input requires a signed-in controller.')
                input_commands(args.get('events'))
                if not tab:
                    await self.ensure_desktop()
                    await self.desktop_input(args['events'])
                    self.foreground_tab = ''
                    self.lease_until = time.monotonic()+60
                    # The existing frame loop captures independently. Input must
                    # not wait for encoding and returning a full desktop JPEG.
                    include_frame = args.get('frame') is not False
                    if include_frame:
                        await self.refresh_frame()
                    return await self.state(include_frame=include_frame)
            if tab:
                page = self.page_for(tab)
                if not page and action == 'open':
                    page = await self.open_review_page(tab)
                if not page:
                    raise ValueError('Open this pull request tab before interacting with it.')
            else:
                await self.open()
                page = self.page
            if tab:
                await self.begin_review_foreground(tab)
                await self.show_review_page(page)
            self.foreground_tab = tab
            if action == 'input':
                await self.browser_input(page, args['events'])
            elif action == 'open':
                if args.get('url'):
                    await page.goto(valid_url(args['url']), wait_until='domcontentloaded')
            elif action == 'click':
                if 'role' in args:
                    await page.get_by_role(args['role'], name=args['name'], exact=True).click()
                else:
                    await page.mouse.click(max(0, min(WIDTH-1, float(args['x']))), max(0, min(HEIGHT-1, float(args['y']))))
            elif action == 'fill':
                await page.get_by_label(args['label'], exact=True).fill(args['value'])
            elif action == 'type':
                await page.keyboard.insert_text(str(args['text'])[:10000])
            elif action == 'key':
                key = args['key']
                if key not in {'Enter', 'Tab', 'Shift+Tab', 'Escape', 'Backspace', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Control+a', 'Meta+a', 'Space'}:
                    raise ValueError('Unsupported browser key.')
                await page.keyboard.press(key)
            elif action == 'scroll':
                await page.mouse.wheel(0, max(-1800, min(1800, int(args['dy']))))
            elif action == 'back':
                await page.go_back(wait_until='domcontentloaded')
            elif action == 'screenshot':
                available = self.budget()
                path = CAPTURES / capture_name(args.get('name'), 'png')
                raw = await page.screenshot(full_page=False) if tab else await asyncio.to_thread(self.desktop_image, 'PNG')
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
                self.recording = {'process': process, 'path': path, 'started': time.time(), 'tab': tab, 'actor': actor}
                await asyncio.sleep(.3)
                if process.returncode is not None:
                    result = await self.stop_recording()
                    return result
                return {'recording': True, 'name': path.with_suffix('.webm').name, 'path': str(path.with_suffix('.webm')), 'max_seconds': 600}
            elif action != 'read':
                raise ValueError('Unknown computer action.')
            await self.refresh_frame(tab)
            if actor != 'agent':
                return await self.state(tab)
            if not tab:
                Path('/artifacts').mkdir(exist_ok=True)
                await page.screenshot(path='/artifacts/browser.png')
            return {'url': page.url, 'title': await page.title(),
                    'text': (await page.locator('body').inner_text())[:24000],
                    'elements': await page.locator('a,button,input,select,textarea').evaluate_all(
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
            if not 0 < length <= PRIVATE_BODY_LIMIT:
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
            return {'available': False, 'tab': valid_tab(body.get('tab', ''))}
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
        line = sys.stdin.buffer.readline(PRIVATE_BODY_LIMIT + 1)
        if not line or len(line) > PRIVATE_BODY_LIMIT:
            return
        try:
            body = json.loads(line)
            result = request(body, start=starts_service(body))
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
        print(json.dumps(request(body, start=starts_service(body))))
