"""Check the real model-picker UI against synthetic API fixtures.

Start: node scripts/settings_ui_preview.cjs --port 8840
Run: python scripts/model_picker_smoke.py
Requires Playwright and Chromium; no provider requests are made.
"""
import os
import shutil

from playwright.sync_api import sync_playwright, expect
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox'])
    page=browser.new_page()
    errors=[]
    page.on('pageerror',lambda e: errors.append(str(e)))
    for width in [1440,768,320]:
        page.set_viewport_size({'width':width,'height':900})
        page.goto('http://127.0.0.1:8840')
        trigger=page.get_by_role('button',name='Model for next message',exact=True)
        expect(trigger).to_contain_text('GPT-6 Astra')
        trigger.click()
        menu=page.get_by_role('listbox',name='Model for next message')
        expect(menu).to_be_visible()
        rect=menu.bounding_box()
        assert rect['x']>=0 and rect['x']+rect['width']<=width,rect
        page.keyboard.press('ArrowDown');page.keyboard.press('Enter')
        expect(trigger).to_contain_text('GPT-6.1 Sol')
        assert page.locator('#new-model').input_value()=='openai/gpt-6.1-sol'
        assert page.evaluate('state.newDraft.model')=='openai/gpt-6.1-sol'
        trigger.press('ArrowUp');page.keyboard.press('End');page.keyboard.press('Enter')
        expect(trigger).to_contain_text('GLM-5.3')
        trigger.click();page.keyboard.press('Home');page.keyboard.press('Escape')
        expect(trigger).to_be_focused();expect(menu).not_to_be_visible()
        expect(trigger).to_contain_text('GLM-5.3')
        trigger.click();page.get_by_role('heading',name='What are we working on?').click()
        expect(menu).not_to_be_visible()
        trigger.click();page.keyboard.press('Tab');expect(menu).not_to_be_visible()
        page.locator('.task-options summary').click()
        page.locator('#mode').select_option('demo');expect(trigger).to_be_disabled()
        page.locator('#mode').select_option('modal');expect(trigger).to_be_enabled()
        print('home passed',width)
    def chat_response(route):
        response=route.fetch()
        data=response.json()
        data.update(chat_enabled=True,model='openai/gpt-6-astra',messages=[])
        route.fulfill(response=response,json=data)
    page.route('**/api/runs/'+'1'*32,chat_response)
    for width in [1440,768,320]:
        page.set_viewport_size({'width':width,'height':900})
        page.goto('http://127.0.0.1:8840/#run='+'1'*32)
        page.reload()
        trigger=page.get_by_role('button',name='Model for next message',exact=True)
        expect(trigger).to_contain_text('GPT-6 Astra')
        page.evaluate("updateChatStatus({model:'anthropic/claude-opus-5-5'})")
        expect(trigger).to_contain_text('Claude Opus 5.5')
        trigger.click()
        rect=page.get_by_role('listbox').bounding_box()
        assert rect['x']>=0 and rect['x']+rect['width']<=width,rect
        page.get_by_role('option',name='GLM-5.3',exact=True).click()
        expect(trigger).to_contain_text('GLM-5.3')
        assert page.locator('#chat-model').input_value()=='fireworks_ai/glm-5p3'
        assert page.evaluate('state.modelDrafts[state.selected]')=='fireworks_ai/glm-5p3'
        page.evaluate("updateChatStatus({model:'openai/gpt-6-astra'})")
        expect(trigger).to_contain_text('GLM-5.3')
        print('chat passed',width)
    print('errors',errors)
    assert not errors
    browser.close()
