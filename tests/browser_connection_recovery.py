"""Run against scripts/connection_recovery_demo.py. Uses real UI/API, mocked provider."""
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page = browser.new_page()
    for width in (1440, 768, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        page.goto('http://127.0.0.1:8830/demo/login')
        page.evaluate("location.hash='#connections'")
        card = page.locator('.connection-card').filter(has=page.get_by_role('heading', name='Linear', exact=True))
        card.get_by_role('button', name='Manage', exact=True).click()
        button = page.get_by_role('button', name='Check connection', exact=True)
        for status in (403, 429):
            button.click()
            expect(page.locator('#connection-error')).to_have_text(f'Local fixture HTTP {status}')
            expect(button).to_be_enabled()
        button.click()
        expect(card).to_contain_text('Verified')
        expect(page.locator('#connection-error')).to_have_text('')
        expect(button).to_be_enabled()
        print(f'PASS: {width}px failure → failure → success clears alert')
    browser.close()
