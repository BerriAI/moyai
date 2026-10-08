"""Real-app skill chip checks. Start browser_account_composer.py --serve, then
PLAYWRIGHT_BROWSERS_PATH=... python tests/browser_skill_chips.py
The signed local fixture uses synthetic identities and demo sessions.
"""
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:8787'


def value(editor):
    return editor.evaluate('(e) => e.value')


def select(editor, start, end=None):
    editor.evaluate('(e, p) => {e.focus(); e.setSelectionRange(...p);}', [start, start if end is None else end])


def checks():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(BASE + '/_test/sign-in')
        editor = page.get_by_label('Message Moyai')
        expect(editor).to_be_visible()
        for scope in ('personal', 'organization'):
            page.evaluate('''async scope => {
              const skills = (await api('/api/skills')).skills;
              if (!skills.some(s => s.name === 'team' && s.scope === scope))
                await api('/api/skills', {method:'POST',body:JSON.stringify({name:'team',
                  scope,description:'Work together on a task',instructions:'Investigate the task and verify the result.',
                  client_id:'chip-test-'+scope})});
            }''', scope)
        page.reload()
        editor.fill('Please /personal:tea')
        expect(page.locator('.skill-inline-option')).to_have_count(1)
        editor.press('Enter')
        expect(editor.locator('.composer-skill')).to_have_text('team')
        expect(editor.locator('.composer-skill [data-skill-icon="team"]')).to_have_count(1)
        assert value(editor) == 'Please /personal:team '
        assert '/personal:' not in editor.inner_text()
        page.keyboard.type('check this')
        editor.press('Shift+Enter')
        page.keyboard.type('Second line')
        assert value(editor) == 'Please /personal:team check this\nSecond line'
        editor.press('Control+z')
        assert value(editor).endswith('Second lin')
        editor.press('Control+Shift+z')
        assert value(editor).endswith('Second line')
        select(editor, len('Please /personal:team'))
        editor.press('Backspace')
        assert value(editor) == 'Please  check this\nSecond line'
        editor.press('Control+z')
        assert value(editor) == 'Please /personal:team check this\nSecond line'
        # Exact scoped serialization when copying/cutting across an atom.
        select(editor, 0, len('Please /personal:team'))
        copied = editor.evaluate(r'''e => {
          const data = new DataTransfer(); e.dispatchEvent(new ClipboardEvent('cut', {clipboardData:data,bubbles:true,cancelable:true}));
          return data.getData('text/plain');
        }''')
        assert copied == 'Please /personal:team'
        assert value(editor) == ' check this\nSecond line'
        editor.press('Control+z')
        # Pasted HTML is ignored, while plain text and newlines survive.
        select(editor, 0, len(value(editor)))
        editor.evaluate(r'''e => {
          const data=new DataTransfer(); data.setData('text/plain','/personal:team hello\nworld');
          data.setData('text/html','<img src=x onerror=alert(1)>');
          e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
        }''')
        assert value(editor) == '/personal:team hello\nworld'
        assert editor.locator('img').count() == 0
        expect(editor.locator('.composer-skill')).to_have_count(1)
        # Both scopes survive dialog insertion and navigation/draft restoration.
        page.get_by_role('button', name='Choose a skill', exact=True).click()
        page.locator('.skill-choice').filter(has_text='Organization').click()
        assert value(editor) == '/org:team /personal:team hello\nworld'
        expect(editor.locator('.composer-skill')).to_have_count(2)
        page.get_by_role('button', name='Settings', exact=True).click()
        page.get_by_role('link', name='Back to workspace', exact=False).click()
        expect(editor.locator('.composer-skill')).to_have_count(2)
        assert value(editor) == '/org:team /personal:team hello\nworld'
        editor.fill('Library draft')
        page.get_by_role('button', name='Skills', exact=True).click()
        page.locator('.skill-card').filter(has_text='Personal').get_by_role('button', name='Use in chat').click()
        expect(editor.locator('.composer-skill')).to_have_count(1)
        assert value(editor) == '/personal:team Library draft'
        for literal in ('`/personal:team`', '```\n/personal:team\n```',
                        'https://example.test/personal:team', '/personal:unknown', '/personal:team/file'):
            editor.fill(literal)
            expect(editor.locator('.composer-skill')).to_have_count(0)
            assert value(editor) == literal
        editor.fill('/goal Fix a bug')
        expect(editor.locator('.composer-skill')).to_have_count(0)
        expect(page.locator('.goal-draft')).to_be_visible()
        # IME updates are left untouched until composition completes.
        editor.fill('/personal:team ')
        editor.evaluate("e => e.dispatchEvent(new CompositionEvent('compositionstart'))")
        page.keyboard.insert_text('調査')
        editor.evaluate("e => e.dispatchEvent(new CompositionEvent('compositionend'))")
        assert value(editor) == '/personal:team 調査'
        # Mobile layout with wrapping and both scopes.
        for width in (1440, 768, 320):
            page.set_viewport_size({'width':width,'height':900})
            editor.fill('/personal:team /org:team Please investigate this task and verify the result.')
            expect(editor.locator('.composer-skill')).to_have_count(2)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert editor.evaluate('e => e.scrollWidth <= e.clientWidth')
        page.set_viewport_size({'width':1440,'height':900})
        # Serialized length, rather than the shorter rendered labels, bounds submission.
        editor.evaluate("e => {e.value='a'.repeat(16001); e.dispatchEvent(new Event('input',{bubbles:true}));}")
        page.get_by_role('button', name='Start session', exact=True).click()
        expect(editor).to_be_visible()
        assert not page.url.split('#')[-1].startswith('run=')
        # Failure keeps chips; real demo creation sends canonical references once.
        editor.fill('/personal:team Investigate this task')
        page.route('**/api/runs', lambda r: r.fulfill(status=503,json={'detail':'Test retry'}) if r.request.method == 'POST' else r.continue_())
        with page.expect_request(lambda r: r.url.endswith('/api/runs') and r.method == 'POST') as req:
            page.get_by_role('button', name='Start session', exact=True).click()
        assert req.value.post_data_json['prompt'] == '/personal:team Investigate this task'
        expect(editor.locator('.composer-skill')).to_have_count(1)
        page.unroute('**/api/runs')
        page.get_by_role('button', name='Start session', exact=True).click()
        followup = page.locator('#followup')
        expect(followup).to_be_visible(timeout=20000)
        followup.fill('/org:tea')
        expect(page.locator('.skill-inline-option')).to_have_count(1)
        followup.press('Enter')
        page.keyboard.type('Verify the result')
        expect(followup.locator('.composer-skill')).to_have_count(1)
        with page.expect_request(lambda r: r.url.endswith('/messages') and r.method == 'POST') as req:
            followup.press('Enter')
        assert req.value.post_data_json['content'] == '/org:team Verify the result'
        expect(followup).to_have_text('')
        # File-only submission still works after replacing native textarea validation.
        with page.expect_file_chooser() as chooser:
            page.get_by_role('button', name='Attach files', exact=True).click()
        chooser.value.set_files({'name':'chip-check.txt','mimeType':'text/plain','buffer':b'Attachment check'})
        expect(page.locator('.draft-attachment small')).to_have_text('16 B')
        with page.expect_request(lambda r: r.url.endswith('/messages') and r.method == 'POST') as req:
            followup.press('Enter')
        assert len(req.value.post_data_json['attachment_ids']) == 1
        assert req.value.post_data_json['content'] == 'Please respond to the attached files and audio transcripts.'
        assert not errors, errors
        print('PASS: chips, scopes, editing, clipboard, multiline, undo, IME, drafts, lengths, responsive layout, real home/follow-up requests')
        browser.close()


if __name__ == '__main__':
    checks()
