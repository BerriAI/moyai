"""Real local demo API; seed a long conversation before running.

SESSION_UI_RUN_ID selects the seeded conversation. Requires Playwright.
"""
import json, os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
    capture=os.environ.get('MOBILE_CAPTURE_DIR')
    context=browser.new_context(viewport={'width':320,'height':720}, **({'record_video_dir':capture,'record_video_size':{'width':320,'height':720}} if capture else {}))
    page=context.new_page()
    page.goto('http://127.0.0.1:8830/demo/login')
    expect(page.locator('#prompt')).to_be_visible()
    page.goto('http://127.0.0.1:8830/#run='+os.environ['SESSION_UI_RUN_ID'])
    followup=page.locator('#followup')
    expect(followup).to_be_visible()
    draft='Scope correction: only adjust the mobile table layout. Preserve the existing API and do not deploy.\nReview the attachment before sending.'
    followup.fill(draft)
    page.locator('input[type=file]').first.set_input_files({'name':'synthetic.txt','mimeType':'text/plain','buffer':b'Synthetic local attachment'})
    preview=page.get_by_role('button',name='Preview synthetic.txt',exact=True)
    expect(preview).to_be_visible()
    measurements=[]
    for width in (320,768,1440):
        page.set_viewport_size({'width':width,'height':720})
        page.locator('#conversation').evaluate('(e)=>{e.scrollTop=0;e.dispatchEvent(new Event("scroll"))}')
        jump=page.locator('#jump-latest')
        expect(jump).to_be_visible()
        measurements.append({'width':width,'jump':jump.bounding_box(),'preview':preview.bounding_box()})
        print(json.dumps(measurements[-1]),flush=True)
        if capture:
            page.screenshot(path=str(Path(capture)/'cycle2714-mobile-verified.png'))
            page.wait_for_timeout(600)
        box=preview.bounding_box()
        preview.click(position={'x':box['width']/2,'y':box['height']-8},timeout=2000)
        dialog=page.locator('dialog[open]')
        expect(dialog).to_be_visible()
        if capture: page.wait_for_timeout(1000)
        page.keyboard.press('Escape')
        expect(dialog).not_to_be_visible()
        expect(followup).to_have_text(draft)
        j=jump.bounding_box(); b=page.locator('.chat-bottom').bounding_box()
        assert j['y']+j['height']<=b['y'], measurements
        jump.click()
        expect(jump).not_to_be_visible()
        if capture:
            page.wait_for_timeout(600)
            break
    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
    print('PASS: preview clicks, draft retention and jump behavior', [m['width'] for m in measurements])
    video=page.video
    context.close()
    if capture:
        target=Path(capture)/'cycle2714-mobile-verified.webm'
        Path(video.path()).replace(target)
        print('Recording:', target)
    browser.close()
