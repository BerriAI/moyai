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
        return await command('input', {'events': list(events)})
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(exercise(parser.parse_args().output))
