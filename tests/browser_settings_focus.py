"""Run against scripts/settings_ui_preview.cjs --port 8840 (synthetic data)."""
import json, os
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.goto('http://127.0.0.1:8840')
    prompt = page.locator('#prompt')
    expect(prompt).to_be_visible()
    prompt.fill('Retain this draft while reviewing connections')
    results = []
    for name in ('Connections', 'Skills'):
        link = page.get_by_role('button', name=name, exact=True) if name == 'Connections' else page.locator('#settings-navigation').get_by_role('link', name=name, exact=True)
        link.focus(); page.keyboard.press('Enter')
        heading = page.locator('#content h1')
        expect(heading).to_be_focused()
        style = heading.evaluate('(e)=>({outline:getComputedStyle(e).outlineStyle,width:getComputedStyle(e).outlineWidth,visible:e.matches(":focus-visible")})')
        results.append({'route': name, **style})
    back = page.get_by_role('link', name='Back to workspace', exact=False)
    back.focus();page.keyboard.press('Enter')
    expect(prompt).to_be_visible()
    expect(prompt).to_have_text('Retain this draft while reviewing connections')
    results.append({'return_focus': page.evaluate('document.activeElement.id || document.activeElement.tagName')})
    print(json.dumps(results))
    assert all(x['outline'] != 'none' and x['width'] != '0px' for x in results[:-1]), results
    expect(prompt).to_be_focused()
    page.screenshot(path=os.environ.get('FOCUS_SCREENSHOT','/workspace/run-20261009T030033Z-2714/focus-verified.png'))
    browser.close()
