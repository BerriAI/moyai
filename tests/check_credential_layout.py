"""Run with Playwright Chromium: python tests/check_credential_layout.py.
Synthetic UI fixture only; no live credentials or backend writes.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    fixture = Path(__file__).parent / 'fixtures' / 'credential-request.html'
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox'])
        for width, height in [(1600, 1000), (1280, 720), (390, 844), (320, 568), (844, 390)]:
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(fixture.resolve().as_uri())
            button = page.get_by_role('button', name='Provide Secret', exact=True)
            assert not page.locator('dialog').evaluate('(el) => el.open')
            for count in [1, 8]:
                page.evaluate('(count) => renderCredentialRequests(Array.from({length:count},(_,i)=>({...request,id:`access-${i}`})))', count)
                assert page.locator('#credential-requests').evaluate('(el)=>!!el.closest("#conversation")')
                assert page.locator('#conversation').bounding_box()['height'] > 100
                assert page.locator('#followup').bounding_box()['y'] < height
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                button.last.click()
                dialog = page.get_by_role('dialog', name='Provide Secret')
                expect(dialog).to_be_visible()
                assert page.get_by_label('Access key ID', exact=True).get_attribute('type') == 'password'
                assert page.get_by_label('Secret access key', exact=True).get_attribute('type') == 'password'
                assert page.locator('#secret-value').count() == 0
                expect(page.get_by_role('radio', name='Personal', exact=True)).to_be_checked()
                page.get_by_role('radio', name='Session only', exact=True).check()
                expect(page.locator('#secret-lifetime')).to_have_value('session')
                page.get_by_role('radio', name='Organization', exact=True).check()
                expect(page.locator('#secret-scope')).to_have_value('organization')
                expect(page.locator('#secret-lifetime')).to_have_value('persistent')
                assert page.locator('input[name="secret-use"]:checked').count() == 1
                page.locator('#secret-input-0').fill('synthetic-draft')
                assert 'synthetic-draft' not in page.evaluate('JSON.stringify(localStorage)')
                page.keyboard.press('Escape')
                page.wait_for_function('!document.querySelector("dialog").open && !document.querySelector("#secret-input-0")')
                assert button.last.evaluate('(el)=>el===document.activeElement')
            page.goto(fixture.resolve().as_uri()+'?token')
            button.click()
            token = page.get_by_label('Access token', exact=True)
            assert token.get_attribute('type') == 'password'
            assert page.locator('#secret-value-fields input').count() == 1
            assert 'Environment variables (JSON)' not in page.locator('#credential-form').inner_text()
            personal = page.get_by_role('radio', name='Personal', exact=True)
            personal.focus()
            page.keyboard.press('ArrowLeft')
            expect(page.get_by_role('radio', name='Session only')).to_be_checked()
            page.wait_for_function('getComputedStyle(document.querySelector(".secret-use-highlight")).transform === "matrix(1, 0, 0, 1, 0, 0)"')
            page.keyboard.press('ArrowRight')
            expect(personal).to_be_checked()
            assert page.locator('.secret-use-highlight').evaluate('(el)=>getComputedStyle(el).transitionDuration') == '0.2s'
            page.emulate_media(reduced_motion='reduce')
            assert page.locator('.secret-use-highlight').evaluate('(el)=>getComputedStyle(el).transitionDuration') == '0s'
            assert page.locator('#credential-dialog').evaluate('(el)=>el.scrollWidth <= el.clientWidth')
            page.get_by_role('button', name='Close credential form').click()
            page.evaluate('renderCredentialRequests([{...request,can_organization:false}])')
            button.click()
            expect(page.get_by_role('radio', name='Organization')).to_be_disabled()
            expect(personal).to_be_checked()
            page.keyboard.press('Escape')
            page.wait_for_function('!document.querySelector("dialog").open')
            page.evaluate('renderCredentialRequests([{...request,can_personal:false,can_organization:false}])')
            assert button.count() == 0
            assert 'Waiting for the requester' in page.locator('#credential-requests').inner_text()
            assert not errors, errors
            print(f'PASS {width}x{height}: inline card, modal, three-way selector, keyboard, animation, permissions, draft clearing')
            page.close()
        browser.close()


if __name__ == '__main__':
    main()
