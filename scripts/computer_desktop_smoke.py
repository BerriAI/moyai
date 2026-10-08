"""Exercise the real Linux desktop with isolated test data and save visible proof.

Run inside the transport image: python /app/scripts/computer_desktop_smoke.py --output /proof
No model, Modal, Substrate cluster or external sign-in is used.
"""
import argparse
import asyncio
import base64
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox import computer

PAGE = '''<!doctype html><meta charset="utf-8"><title>Desktop sign-in demo</title>
<style>body{margin:0;background:#f5f3f8;color:#302b3b;font:18px system-ui;display:grid;place-items:center;height:100vh}
main{background:white;padding:36px;width:400px;border:1px solid #ded8e8;border-radius:16px}h1{font-size:26px}
label{display:block;margin-top:20px}input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #bfb7cc;border-radius:7px;font:inherit}
button{margin-top:24px;padding:12px 24px;background:#655095;border:0;border-radius:7px;color:white;font:inherit}
p{font-size:14px;color:#70677e}output{display:block;margin-top:20px;color:#30704c}</style>
<main><h1>Sign in to your workspace</h1><p>Local demo · test details only</p>
<form onsubmit="event.preventDefault();document.querySelector('output').textContent='Signed in as '+this.email.value">
<label>Email<input name="email" type="email" autocomplete="off"></label>
<label>Password<input name="password" type="password" autocomplete="off"></label><button>Continue</button></form><output></output></main>'''


async def wake_processes(output: Path) -> None:
    """Fresh clients must observe the service after the wake client exits.

    Run in a disposable container; its lifetime also bounds the detached service.
    """
    output.mkdir(parents=True, exist_ok=True)
    async def request(action, bridge=False):
        body = json.dumps({'action': action})
        args = ['bridge'] if bridge else ['request', body]
        process = await asyncio.create_subprocess_exec(sys.executable, computer.__file__, *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate((body+'\n').encode() if bridge else None), 540)
        assert process.returncode == 0, stderr.decode()
        return json.loads(stdout)
    assert not (await request('state'))['available']
    assert (await request('wake'))['available']
    for bridge in (False, True):
        state = await request('state', bridge)
        assert state['available'] and state['frame'] and not state['controller']
    (output/'wake-live.jpg').write_bytes(base64.b64decode(state['frame']))
    (output/'wake-verification.txt').write_text('PASS: wake client exits; fresh request and bridge clients retain live desktop/frame without taking control\n')
    print((output/'wake-verification.txt').read_text(), flush=True)


async def exercise(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    computer.CAPTURES = output
    desktop = computer.Computer()
    service = asyncio.create_task(computer.serve(desktop))
    # Exercise the same persistent pipe used by the app, while retaining direct
    # read-only assertions against the service's real Chromium context.
    for attempt in range(100):
        try:
            _, writer = await asyncio.open_connection('127.0.0.1', computer.PORT)
            writer.close()
            await writer.wait_closed()
            break
        except ConnectionRefusedError:
            await asyncio.sleep(.02)
    else:
        raise RuntimeError('Desktop service did not start.')
    pipe = await asyncio.create_subprocess_exec(sys.executable, computer.__file__, 'bridge',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=2*1024*1024)
    actor = 'desktop-test-person'
    async def command(action, args=None):
        pipe.stdin.write((json.dumps({'action': action, 'actor': actor, 'args': args or {}})+'\n').encode())
        await pipe.stdin.drain()
        result = json.loads(await asyncio.wait_for(pipe.stdout.readline(), 60))
        if result.get('error'):
            raise RuntimeError(result['error'])
        return result
    async def inputs(*events):
        result = await command('input', {'events': list(events), 'frame': False})
        assert 'frame' not in result and result['surface'] == 'desktop'
        return result
    def key(value):
        return {'type': 'key', 'key': value}
    def text(value):
        return {'type': 'text', 'text': value}

    with tempfile.TemporaryDirectory() as directory:
        Path(directory, 'index.html').write_text(PAGE)
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/'
        try:
            assert not (await command('state'))['available']
            awake = await command('wake')
            assert awake['available'] and not awake['controller']
            await command('claim')
            await command('open')
            await asyncio.sleep(1)
            await command('record_start', {'name': 'desktop-demo'})
            # URL is entered into real browser chrome, never page.goto.
            await inputs(key('Control+l'), text(url))
            await asyncio.sleep(2)
            await inputs(key('Enter'))
            await desktop.page.wait_for_url(url)
            await desktop.page.wait_for_selector('input[name=email]')
            await asyncio.sleep(1)
            assert (await desktop.state())['surface'] == 'desktop'
            first = desktop.page
            # Native clicks use display coordinates including browser chrome.
            box = await first.locator('input[name=email]').bounding_box()
            chrome = await first.evaluate('({x:screenX,y:screenY,top:outerHeight-innerHeight})')
            point = {'x': chrome['x']+box['x']+40, 'y': chrome['y']+chrome['top']+box['y']+20, 'button': 0}
            await inputs({'type': 'pointer', 'phase': 'down', **point}, {'type': 'pointer', 'phase': 'up', **point},
                         text('demo.person@example.com'))
            await asyncio.sleep(2)
            assert await first.locator('input[name=email]').input_value() == 'demo.person@example.com'
            await inputs(key('Control+a'), {'type': 'paste', 'text': 'demo@example.com\n'}, key('Tab'), text('sample-password'))
            assert await first.locator('output').inner_text() == ''
            assert await first.locator('input[name=email]').input_value() == 'demo@example.com'
            assert await first.locator('input[name=password]').input_value() == 'sample-password'
            await asyncio.sleep(2)
            await inputs(key('Tab'), key('Enter'))
            await first.locator('output').filter(has_text='Signed in as demo@example.com').wait_for()
            await command('screenshot', {'name': 'desktop-signin'})
            await asyncio.sleep(2)
            # A second native tab and switching back must update the agent's target.
            async with desktop.context.expect_page() as new_page:
                await inputs(key('Control+t'))
            second = await new_page.value
            await inputs(key('Control+l'), text(url+'?second-tab'), key('Enter'))
            await second.wait_for_url(url+'?second-tab')
            await asyncio.sleep(1)
            await inputs(key('Control+Shift+Tab'))
            await asyncio.sleep(1)
            await command('release')
            result = await desktop.command({'action': 'read'})
            assert result['url'] == url, result['url']
            assert 'Signed in as demo@example.com' in result['text']
            await command('claim')
            await inputs({'type': 'pointer', 'phase': 'down', **point}, {'type': 'pointer', 'phase': 'up', **point},
                         key('Control+a'), text('café 日本語'))
            assert await first.locator('input[name=email]').input_value() == 'café 日本語'
            await inputs(key('Control+a'), text('demo@example.com'))
            await asyncio.sleep(1)
            await command('record_stop')
            await inputs(key('Control+w'), key('Control+w'))
            await asyncio.sleep(.5)
            assert (await desktop.state())['available']
            await command('open')
            assert desktop.page and not desktop.page.is_closed()
            await desktop.refresh_frame()
            (output/'desktop-final.jpg').write_bytes(base64.b64decode(desktop.frame))
            (output/'verification.txt').write_text('PASS: native address bar navigation; pointer click; direct typing and Unicode; multiline paste without submission; selection/replacement; Tab; password field; form submission; new tab; agent handoff follows selected tab; reopen after closing all tabs; desktop screenshot; real recording\n')
            print((output/'verification.txt').read_text(), flush=True)
        except Exception:
            if desktop.desktop:
                (output/'failure.png').write_bytes(await asyncio.to_thread(desktop.desktop_image, 'PNG'))
            raise
        finally:
            pipe.stdin.close()
            await asyncio.wait_for(pipe.wait(), 5)
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)
            await desktop.stop_recording()
            if desktop.browser:
                await desktop.browser.close()
            if desktop.playwright:
                await desktop.playwright.stop()
            for process in [desktop.panel, desktop.desktop, desktop.display]:
                if process and process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, 5)
            server.shutdown()


async def browser_resume(output: Path) -> None:
    """Real Chromium state and private pipe across a cold browser/service restart."""
    output.mkdir(parents=True, exist_ok=True)
    scope = 'a' * 32
    page_html = PAGE.replace(
        "document.querySelector('output').textContent='Signed in as '+this.email.value",
        "document.cookie='demo=yes; SameSite=Lax'; localStorage.setItem('demo','yes'); sessionStorage.setItem('demo','yes'); "
        "document.querySelector('output').textContent='Signed in as '+this.email.value")
    page_html += '''<script>if(document.cookie.includes('demo=yes') && localStorage.demo==='yes' && sessionStorage.demo==='yes')
        document.querySelector('output').textContent='Signed in as demo@example.com';</script>'''
    desktop = computer.Computer()
    service = asyncio.create_task(computer.serve(desktop))
    recorder = None
    async def private(operation, state=None):
        body = {'action': 'state', 'args': {'browser': operation, 'scope': scope}}
        if operation == 'restore':
            body['args']['state'] = state
        # An actual new stdin bridge client, including the >64 KiB restore.
        process = await asyncio.create_subprocess_exec(sys.executable, computer.__file__, 'bridge',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate((json.dumps(body)+'\n').encode())
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert not result.get('error'), result.get('error')
        return result
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, 'index.html').write_text(page_html)
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/'
        try:
            await private('restore')
            await desktop.command({'action': 'open', 'args': {'url': url}})
            recorder = await asyncio.create_subprocess_exec('ffmpeg', '-y', '-loglevel', 'error', '-f', 'x11grab',
                '-video_size', f'{computer.WIDTH}x{computer.HEIGHT}', '-framerate', '15', '-i', ':99.0', '-an',
                '-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '6', '-b:v', '700k',
                str(output/'browser-resume.webm'), stdin=asyncio.subprocess.PIPE)
            await asyncio.sleep(2)
            await desktop.command({'action': 'fill', 'args': {'label': 'Email', 'value': 'demo@example.com'}})
            await desktop.command({'action': 'fill', 'args': {'label': 'Password', 'value': 'sample-password'}})
            await desktop.command({'action': 'click', 'args': {'role': 'button', 'name': 'Continue'}})
            await desktop.page.evaluate('''async () => {
                localStorage.large = 'x'.repeat(90000);
                const db = await new Promise(resolve => { const r = indexedDB.open('auth', 1);
                    r.onupgradeneeded = () => r.result.createObjectStore('tokens'); r.onsuccess = () => resolve(r.result); });
                await new Promise(resolve => { const t = db.transaction('tokens', 'readwrite');
                    t.objectStore('tokens').put('signed-in', 'demo'); t.oncomplete = resolve; }); db.close();
            }''')
            first = desktop.page
            second = await desktop.context.new_page()
            await second.goto(url+'?second-tab')
            await second.evaluate("sessionStorage.demo='second-tab'")
            await first.bring_to_front()
            # Review tabs carry separate state, never included in the checkpoint.
            review = await desktop.browser.new_context()
            await review.add_cookies([{'name': 'private-review', 'value': 'excluded', 'url': url}])
            desktop.review_context = review
            saved = (await private('checkpoint'))['state']
            assert len(json.dumps(saved)) > 65536
            assert [p['session_storage']['demo'] for p in saved['pages']] == ['yes', 'second-tab']
            assert not any(c['name'] == 'private-review' for c in saved['storage']['cookies'])
            (output/'browser-before-restart.png').write_bytes(await asyncio.to_thread(desktop.desktop_image, 'PNG'))
            await asyncio.sleep(3)
            await desktop.browser.close()
            await desktop.playwright.stop()
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)
            # No Python or browser object survives; only the private checkpoint.
            previous = desktop
            desktop = computer.Computer()
            service = asyncio.create_task(computer.serve(desktop))
            await private('restore', saved)
            await desktop.open()
            assert await desktop.page.locator('output').inner_text() == 'Signed in as demo@example.com'
            assert await desktop.context.pages[1].evaluate('sessionStorage.demo') == 'second-tab'
            token = await desktop.page.evaluate('''async () => { const db = await new Promise(resolve => {
                const r = indexedDB.open('auth', 1); r.onsuccess = () => resolve(r.result); });
                const value = await new Promise(resolve => { const r = db.transaction('tokens').objectStore('tokens').get('demo');
                    r.onsuccess = () => resolve(r.result); }); db.close(); return value; }''')
            assert token == 'signed-in'
            # A reconnect must not overwrite newer live state with the old snapshot.
            await desktop.page.evaluate("localStorage.current='newer'")
            await private('restore', saved)
            assert await desktop.page.evaluate('localStorage.current') == 'newer'
            (output/'browser-after-restart.png').write_bytes(await asyncio.to_thread(desktop.desktop_image, 'PNG'))
            await asyncio.sleep(4)
            await desktop.page.evaluate("sessionStorage.clear();localStorage.clear();document.cookie='demo=;Max-Age=0'")
            await desktop.page.reload()
            assert await desktop.page.locator('output').inner_text() == ''
            assert await desktop.page.evaluate('sessionStorage.demo') is None
            logout = (await private('checkpoint'))['state']
            assert logout['storage']['cookies'] == []
            await desktop.browser.close()
            await desktop.playwright.stop()
            (output/'browser-verification.txt').write_text(
                'PASS: session cookies, localStorage, IndexedDB, per-tab sessionStorage and active tab survive cold browser/service restart; '
                '>64 KiB private stdin restore; reconnect preserves newer live state; logout stays logged out; PR context excluded\n')
            print((output/'browser-verification.txt').read_text(), flush=True)
        finally:
            if recorder and recorder.returncode is None:
                recorder.stdin.write(b'q\n')
                await recorder.stdin.drain()
                await recorder.wait()
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)
            if desktop.browser and desktop.browser.is_connected():
                await desktop.browser.close()
            for owner in (locals().get('previous'), desktop):
                if owner:
                    for process in (owner.panel, owner.desktop, owner.display):
                        if process and process.poll() is None:
                            process.terminate()
            server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--wake-only', action='store_true', help='Verify desktop ownership across fresh command processes')
    parser.add_argument('--persistence-only', action='store_true', help='Verify authenticated browser recovery with real Chromium')
    args = parser.parse_args()
    asyncio.run(browser_resume(args.output) if args.persistence_only else wake_processes(args.output) if args.wake_only else exercise(args.output))
