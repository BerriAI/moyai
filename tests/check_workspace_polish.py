"""Browser regression for the real local app in demo mode (no provider calls).

Start an isolated local app, then run:
  uv run --with playwright python tests/check_workspace_polish.py
Optional: MOYAI_TEST_URL overrides http://127.0.0.1:8787.
"""
import os
from playwright.sync_api import sync_playwright, expect


def fits(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Page overflows horizontally'
    composer = page.locator('#message-form').bounding_box()
    assert composer and composer['y'] >= 0
    assert composer['y'] + composer['height'] <= page.viewport_size['height']
    assert page.locator('#conversation').bounding_box()['height'] > 100
    assert page.locator('#message-form').evaluate('(el) => el.scrollWidth <= el.clientWidth')


def main():
    url = os.environ.get('MOYAI_TEST_URL', 'http://127.0.0.1:8787')
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1600, 'height': 960})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        page.get_by_label('Message Moyai Devin', exact=True).fill('Workspace polish browser regression — local demo only')
        # Explicitly select demo; never rely on the server's execution default.
        page.locator('.task-options > summary').click()
        page.locator('#mode').select_option('demo')
        page.locator('.task-options > summary').click()
        page.get_by_role('button', name='Start session', exact=True).click()
        expect(page.locator('#run-status')).to_have_text('Ready', timeout=30000)
        session_url = page.url
        expect(page.locator('.chat-message.assistant')).to_contain_text('Demo complete')
        assert page.locator('.rail').bounding_box()['width'] == 300
        assert page.locator('.chat-message.user').evaluate('(el) => getComputedStyle(el).backgroundColor') == 'rgb(244, 244, 244)'
        for width, height in [(1600, 960), (1280, 800), (1024, 768), (844, 390), (390, 844), (320, 568)]:
            page.set_viewport_size({'width': width, 'height': height})
            fits(page)
            page.get_by_role('button', name='Activity', exact=True).click()
            panel = page.locator('#workspace-panel')
            expect(panel).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            if width > 760:
                fits(page)
                header = page.locator('.topbar').bounding_box()
                tabs = page.locator('.panel-header').bounding_box()
                assert tabs['y'] == header['y'] == 0
                assert abs(header['x'] + header['width'] - tabs['x']) <= 2, (width, header, tabs)
                grip = page.get_by_role('separator', name='Resize workspace panel')
                grip.focus()
                grip.press('ArrowLeft')
                header = page.locator('.topbar').bounding_box()
                tabs = page.locator('.panel-header').bounding_box()
                assert abs(header['x'] + header['width'] - tabs['x']) <= 2, (width, header, tabs)
                page.get_by_role('button', name='Expand workspace panel', exact=True).click()
                assert not page.locator('.chat-panel').is_visible()
                page.get_by_role('button', name='Restore panel size', exact=True).click()
            else:
                expect(page.locator('.chat-panel')).not_to_be_visible()
            panel.get_by_role('button', name='Hide workspace panel', exact=True).click()
            fits(page)
            if width <= 850:
                page.get_by_role('button', name='Open sidebar', exact=True).click()
                expect(page.get_by_role("searchbox", name="Search sessions")).to_be_focused()
                page.locator('#close-sidebar').click()
                expect(page.locator('#sidebar')).to_have_attribute('inert', '')
            print(f'PASS {width}x{height}: composer, overflow, panel, resize, sidebar')
        page.set_viewport_size({'width': 1600, 'height': 960})
        page.get_by_label('Message Moyai Devin', exact=True).fill('Verify a follow-up still sends through the real demo backend.')
        page.get_by_role('button', name='Send message', exact=True).click()
        expect(page.locator('.chat-message.assistant')).to_have_count(2, timeout=30000)
        page.get_by_role('button', name='Activity', exact=True).click()
        page.reload()
        expect(page.locator('#workspace-panel')).to_be_visible()
        expect(page.locator('.chat-message.assistant')).to_have_count(2)
        assert page.url == session_url
        assert not errors, errors
        # Desktop sidebar can be hidden and restored from the header.
        page.get_by_role('button', name='Close sidebar', exact=True).last.click()
        expect(page.locator('#sidebar')).to_be_hidden()
        page.get_by_role('button', name='Open sidebar', exact=True).click()
        expect(page.locator('#sidebar')).to_be_visible()
        page.get_by_role('button', name='Search sessions', exact=True).click()
        expect(page.get_by_role('searchbox', name='Search sessions')).to_be_focused()
        # Inter comes from Google Fonts (CSP-allowed); system fonts are the offline fallback.
        assert 'Inter' in page.evaluate("getComputedStyle(document.body).fontFamily")
        print('PASS real demo submit/follow-up, persisted tab, reload, no browser errors')
        browser.close()


if __name__ == '__main__':
    main()
