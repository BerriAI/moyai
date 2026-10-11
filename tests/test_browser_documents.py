"""Real Chromium contract tests for complete observations and exact DOM targets.

Run with uv run --frozen --with playwright==1.56.0 pytest tests/test_browser_documents.py.
"""
import base64
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sandbox.browser_documents import BrowserDocuments, workspace_file
from sandbox.computer import Computer


@pytest.fixture
async def browser():
    playwright = pytest.importorskip('playwright.async_api')
    async with playwright.async_playwright() as api:
        instance = await api.chromium.launch(headless=True, executable_path=os.environ.get('MOYAI_TEST_CHROMIUM'))
        yield instance
        await instance.close()


async def test_complete_snapshot_ranges_frames_shadow_dom_and_stale_targets(browser):
    context = await browser.new_context()
    page = await context.new_page()
    text = 'Full source evidence 界. '*2000+'Final condition.'
    await page.set_content('<p>'+text+'</p>'+''.join(f'<button>Duplicate</button>' for _ in range(160))+
                           '<iframe srcdoc="<button>Inside frame</button>"></iframe><div id="shadow"></div>')
    await page.evaluate("document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML='<button>Shadow action</button>'")
    documents = BrowserDocuments()
    first = await documents.read(page, {'characters': 12000, 'element_count': 20})
    assert first['characters'] > 24000 and first['element_total'] == 161
    # Later reads of an observation stay consistent even after the live DOM changes.
    await page.locator('p').evaluate("e => e.textContent='changed live page'")
    chunks, offset = [first['text']], first['next_offset']
    while offset is not None:
        result = await documents.read(page, {'snapshot': first['snapshot'], 'offset': offset, 'characters': 12000})
        chunks.append(result['text'])
        offset = result['next_offset']
    assert text in ''.join(chunks)
    controls = await documents.read(page, {'snapshot': first['snapshot'], 'element_offset': 150})
    assert controls['elements'][-1]['name'] == 'Shadow action'
    late = controls['elements'][0]['target']
    await (await documents.target(page, late)).click()
    assert await page.locator('button').nth(150).evaluate('e => e===document.activeElement')
    await page.locator('button').nth(150).evaluate("e => e.replaceWith(e.cloneNode(true))")
    with pytest.raises(ValueError, match='changed'):
        await documents.target(page, late)
    child = next(frame for frame in first['frames'] if not frame['main'])
    nested = await documents.read(page, {'frame': child['id']})
    assert nested['elements'][0]['name'] == 'Inside frame'
    await (await documents.target(page, nested['elements'][0]['target'])).click()
    other = await context.new_page()
    with pytest.raises(ValueError, match='stale'):
        await documents.target(other, nested['elements'][0]['target'])
    await context.close()


async def test_real_computer_tabs_select_upload_download_and_full_text(browser, tmp_path):
    context = await browser.new_context()
    page = await context.new_page()
    payload = b'\x00binary download\xff'
    await page.set_content('<label>Notes<textarea></textarea></label><input type="file">'
        '<select><option value="a">First</option><option value="b">Second</option></select>'
        '<a download="evidence.bin" href="data:application/octet-stream;base64,'+base64.b64encode(payload).decode()+'">Download</a>')
    computer = Computer(tmp_path, tmp_path/'artifacts')
    computer.context, computer.page = context, page
    computer.open = AsyncMock()
    computer.refresh_frame = AsyncMock()
    computer.settle_foreground = AsyncMock()
    async def call(action, **args):
        return await computer.command({'action': action, 'actor': 'agent', 'args': args})
    observed = await call('read')
    target = {item['tag']: item['target'] for item in observed['elements']}
    note = 'Keep all text. '*1200+'Last line.'
    await call('fill', target=target['TEXTAREA'], value=note)
    assert await page.locator('textarea').input_value() == note
    await call('select', target=target['SELECT'], values=['b'])
    assert await page.locator('select').input_value() == 'b'
    upload = tmp_path/'evidence.bin'
    upload.write_bytes(payload)
    await call('upload', target=target['INPUT'], paths=[str(upload)])
    assert await page.locator('input').evaluate('e => e.files[0].size') == len(payload)
    downloaded = await call('download', target=target['A'])
    assert Path(downloaded['path']).read_bytes() == payload
    second = await context.new_page()
    await second.set_content('<button>Other tab</button>')
    tabs = await call('tabs')
    selected = next(item for item in tabs['tabs'] if not item['active'])
    result = await call('select_tab', page_id=selected['id'])
    assert result['elements'][0]['name'] == 'Other tab' and computer.page is second
    await context.close()


def test_upload_cannot_read_outside_workspace(tmp_path):
    root = tmp_path/'workspace'
    root.mkdir()
    private = tmp_path/'private'
    private.write_text('private')
    (root/'escape').symlink_to(private)
    for path in ('../private', 'escape'):
        with pytest.raises(ValueError, match='inside'):
            workspace_file(path, root)


def test_screenshot_reaches_the_model_as_image_content(tmp_path, monkeypatch):
    from agent.tools import mcp_bridge
    capture = tmp_path/'observed.png'
    capture.write_bytes(b'\x89PNG\r\n\x1a\nfixture-image')
    monkeypatch.setattr(mcp_bridge.computer, 'CAPTURES', tmp_path)
    monkeypatch.setattr(mcp_bridge, 'browser_tool', lambda *_: {'path': str(capture), 'name': capture.name})
    result = mcp_bridge.call_tool({'name': 'browser_screenshot', 'arguments': {'name': 'observed'}}, mcp_bridge.BROWSER_TOOLS)
    assert result['isError'] is False
    assert result['content'][1]['type'] == 'image' and result['content'][1]['mimeType'] == 'image/png'
    assert base64.b64decode(result['content'][1]['data']) == capture.read_bytes()
