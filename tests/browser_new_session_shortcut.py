"""Shortcut regression against the real frontend and local synthetic API fixtures.

Run scripts/settings_ui_preview.cjs --port 8840, then run this with Playwright.
No provider requests or session creation are performed.
"""
import os
from playwright.sync_api import expect, sync_playwright


def checks():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_EXECUTABLE', '/usr/bin/chromium'),
            args=['--no-sandbox', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
        )
        page = browser.new_page()
        errors, writes = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: writes.append(request.url)
                if request.method == 'POST' and request.url.endswith('/api/runs') else None)

        # Exercise Auto · Codex and an explicit override using synthetic configuration.
        def config(route):
            data = route.fetch().json()
            data['harness'] = 'codex'
            data['harnesses'].append({'id': 'codex', 'name': 'Codex'})
            data['models'][0]['default_harness'] = 'codex'
            route.fulfill(json=data)
        page.route('**/api/config', config)
        page.goto('http://127.0.0.1:8840')
        prompt = page.get_by_label('Message Moyai', exact=True)
        engine = page.get_by_label('Engine for new session', exact=True)
        expect(prompt).to_be_visible()
        expect(engine.locator('option:checked')).to_have_text('Auto · Codex')
        prompt.fill('Keep this unsent draft')
        for width in (1440, 768, 320):
            page.set_viewport_size({'width': width, 'height': 900})
            page.evaluate("""() => {
                window.originalNodes = ['#task-form','#prompt','#new-harness','#new-model','.record-button']
                    .map(selector => [selector, document.querySelector(selector)]);
                window.removedComposerNodes = [];
                window.composerObserver = new MutationObserver(records => records.forEach(record =>
                    record.removedNodes.forEach(node => {
                        if(originalNodes.some(([,original]) => node === original || node.contains(original)))
                            removedComposerNodes.push(node.nodeName);
                    })));
                composerObserver.observe(document.querySelector('#content'), {subtree:true,childList:true});
            }""")
            for key in ('Alt+k', 'Control+k', 'Meta+k') * 3:
                engine.focus()
                page.keyboard.press(key)
                expect(prompt).to_be_focused()
                expect(prompt).to_have_text('Keep this unsent draft')
                engine.click()
                page.keyboard.press('ArrowDown')
                page.keyboard.press('Enter')
                expect(engine).to_have_value('claude-agent-sdk')
                engine.select_option('')
                expect(engine.locator('option:checked')).to_have_text('Auto · Codex')
            # macOS Option+K reports a composed character with physical code KeyK.
            page.evaluate("window.dispatchEvent(new KeyboardEvent('keydown', {key:'˚',code:'KeyK',altKey:true,bubbles:true,cancelable:true}))")
            expect(prompt).to_be_focused()
            assert page.evaluate('originalNodes.every(([selector,node]) => node === document.querySelector(selector))')
            assert page.evaluate('removedComposerNodes') == []
            page.evaluate('composerObserver.disconnect()')
            print(f'PASS: {width}px; repeated Alt/Ctrl/Meta+K; native picker selection; same composer/mic nodes; draft intact')

        page.set_viewport_size({'width': 1440, 'height': 900})
        page.emulate_media(reduced_motion='reduce')
        page.get_by_role('button', name='Record audio', exact=True).click()
        expect(page.get_by_role('button', name='Stop recording', exact=True)).to_be_visible()
        page.keyboard.press('Alt+k')
        expect(page.get_by_role('button', name='Stop recording', exact=True)).to_be_visible()
        page.get_by_role('button', name='Cancel recording', exact=True).click()
        page.locator('#new-task').click()
        assert page.evaluate('originalNodes.every(([selector,node]) => node === document.querySelector(selector))')
        engine.select_option('codex')
        for destination in ('session', 'settings'):
            if destination == 'session':
                page.locator('[data-run]').first.click()
            else:
                page.get_by_role('button', name='Settings', exact=True).click()
            expect(prompt).to_have_count(0)
            page.keyboard.press('Alt+k')
            expect(prompt).to_be_focused()
            expect(prompt).to_have_text('Keep this unsent draft')
            expect(engine).to_have_value('codex')
        assert not errors, errors
        assert not writes, writes
        print('PASS: active recording preserved; click reuses form; navigation returns from session/settings; no JS errors or session writes')
        browser.close()


if __name__ == '__main__':
    checks()
