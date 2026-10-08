"""Real Chromium check: run scripts/pull_request_panel_demo.py --port 8846 first.

Uses the real frontend/API with synthetic sessions. Requires Playwright and Chromium.
SUBAGENT_UI_URL and CHROMIUM_PATH override local defaults.
"""
import os

from playwright.sync_api import expect, sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(os.environ.get('SUBAGENT_UI_URL', 'http://localhost:8846') + '/demo/login')
        page.locator('#followup').wait_for()
        parent_url = page.url
        card = page.get_by_role('complementary', name='Session toolbox', exact=True)
        expect(card).to_be_visible()
        expect(card.locator('.subagent-row')).to_have_count(3)
        expect(card.locator('[data-pr-url]')).to_have_count(3)
        expect(card).to_contain_text('Failed')
        heights = card.locator('.pull-request-row').evaluate_all(
            '(rows)=>rows.map(row=>row.getBoundingClientRect().height)')
        print('Toolbox row heights:', heights)
        assert max(heights[:3]) <= 82, heights
        assert all(44 <= height <= 46 for height in heights[3:]), heights
        running = card.locator('.subagent-row').filter(has_text='Live updates')
        page.locator('#followup').fill('Keep this unsent draft')
        for status, label in [('running', 'Working now'), ('completed', 'Completed'), ('failed', 'Failed'), ('running', 'Working now')]:
            # Mutate the fixture's database, then wait for normal app polling.
            page.evaluate("status=>api('/demo/agent-state/'+status,{method:'POST'})", status)
            running.focus()
            expect(running).to_contain_text(label, timeout=20000)
            expect(running).to_be_focused()
            expect(page.locator('#followup')).to_have_text('Keep this unsent draft')
        print('PASS current states, natural polling, focused links and composer draft')

        running.press('Enter')
        expect(page).not_to_have_url(parent_url)
        page.get_by_role('button', name='Parent session', exact=False).click()
        expect(page).to_have_url(parent_url)
        expect(card.locator('.subagent-row')).to_have_count(3)
        expect(page.locator('#followup')).to_have_text('Keep this unsent draft')
        print('PASS keyboard child navigation and parent return')

        for width in (1440, 768, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            page.emulate_media(reduced_motion='reduce')
            if width <= 1100:
                expect(card).not_to_be_visible()
            page.get_by_role('button', name='Subagents', exact=True).click()
            panel = page.locator('.panel-agents')
            expect(panel).to_be_visible()
            expect(panel.locator('.subagent-row')).to_have_count(3)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), width
            assert panel.evaluate('(e)=>e.scrollWidth<=e.clientWidth'), width
            expect(page.locator('#followup')).to_have_text('Keep this unsent draft')
            for label, selector in [('Pull requests', '.panel-pulls'), ('Subagents', '.panel-agents')]:
                page.get_by_role('button', name=label, exact=True).click()
                surface = page.locator(selector)
                expect(surface).to_be_visible()
                row = surface.locator('.pull-request-row').first
                # Stress unbroken identifiers without relying on fixture title lengths.
                title = row.locator('strong')
                title.evaluate("e=>e.textContent='LongIdentifier'.repeat(30)")
                assert title.evaluate('(e)=>e.scrollWidth<=e.clientWidth'), width
                assert title.evaluate('(e)=>e.scrollHeight<=e.clientHeight'), width
                assert surface.evaluate('(e)=>e.scrollWidth<=e.clientWidth'), width
                row.focus()
                expect(row).to_be_focused()
                assert row.bounding_box()['height'] >= 44
            page.reload()
            expect(page.locator('.panel-agents')).to_be_visible()
            page.locator('.workspace-panel [data-hide]').click()
            page.locator('#followup').fill('Keep this unsent draft')
            print(f'PASS {width}px: accessible tab, persistence, no overflow, draft preserved')
        assert not errors, errors
        browser.close()


if __name__ == '__main__':
    main()
