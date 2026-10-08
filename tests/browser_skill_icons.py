"""Run against browser_account_composer.py --serve (synthetic signed identity)."""
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:8787'


def checks():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width':1440, 'height':900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(BASE+'/_test/sign-in')
        page.get_by_role('button', name='Skills', exact=True).click()
        page.get_by_role('button', name='Add skill', exact=True).click()
        page.get_by_label('Name', exact=True).fill('icon-demo')
        page.get_by_label('Where should this skill be saved?').select_option('personal')
        page.get_by_label('Icon', exact=True).select_option('video')
        expect(page.locator('#skill-icon-preview [data-skill-icon="video"]')).to_have_count(1)
        page.get_by_label('When should Moyai use this skill?').fill('Verify skill icon rendering')
        page.get_by_label('Markdown instructions', exact=True).fill('Verify the chosen icon.')
        page.get_by_role('button', name='Save skill', exact=True).click()
        card = page.locator('.skill-card').filter(has_text='/personal:icon-demo')
        expect(card.locator('[data-skill-icon="video"]')).to_have_count(1)
        skill = page.evaluate("async()=> (await api('/api/skills')).skills.find(s=>s.name==='icon-demo')")
        assert skill['icon'] == 'video'
        page.reload()
        card.get_by_role('button', name='Edit', exact=True).click()
        expect(page.get_by_label('Icon', exact=True)).to_have_value('video')
        page.get_by_label('Icon', exact=True).select_option('chart')
        page.get_by_role('button', name='Save skill', exact=True).click()
        expect(card.locator('[data-skill-icon="chart"]')).to_have_count(1)
        card.get_by_role('button', name='Use in chat', exact=True).click()
        editor = page.get_by_label('Message Moyai')
        expect(editor.locator('[data-skill-icon="chart"]')).to_have_count(1)
        assert editor.evaluate('e=>e.value') == '/personal:icon-demo '
        editor.fill('/personal:icon-de')
        expect(page.locator('.skill-inline-option [data-skill-icon="chart"]')).to_have_count(1)
        editor.press('Enter')
        expect(editor.locator('[data-skill-icon="chart"]')).to_have_count(1)
        editor.fill('')
        page.get_by_role('button', name='Choose a skill', exact=True).click()
        choice = page.locator('.skill-choice').filter(has_text='icon-demo')
        expect(choice.locator('[data-skill-icon="chart"]')).to_have_count(1)
        choice.click()
        expect(editor.locator('[data-skill-icon="chart"]')).to_have_count(1)
        for width in (1440, 768, 320):
            page.set_viewport_size({'width':width, 'height':900})
            editor.fill('/personal:icon-demo Please verify this skill.')
            expect(editor.locator('[data-skill-icon="chart"]')).to_have_count(1)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        editor.fill('/go')
        expect(page.locator('.skill-inline-option [data-skill-icon="target"]')).to_have_count(1)
        assert not errors, errors
        print('PASS: icon create/edit/reload, API persistence, library, slash picker, dialog, chips, goal and responsive layouts')
        browser.close()


if __name__ == '__main__':
    checks()
