"""Check the real session UI with synthetic API data.

Start: PORT=8768 node tests/preview_session_titles.cjs --serve
Run: python tests/check_sidebar_density.py
DENSITY_URL selects a baseline server for the same regression check.
CHROMIUM_EXECUTABLE selects system Chromium; otherwise use Playwright's install.
"""

import os
from playwright.sync_api import sync_playwright, expect

url = os.environ.get("DENSITY_URL", "http://localhost:8768")
with sync_playwright() as p:
    b = p.chromium.launch(
        executable_path=os.environ.get("CHROMIUM_EXECUTABLE"), args=["--no-sandbox"]
    )
    page = b.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    for width in (1440, 768, 320):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(url)
        page.locator(".session-link").first.wait_for(state="attached")
        if width < 850:
            page.get_by_role("button", name="Open sidebar", exact=True).click()
        disclosure = page.locator(".agent-disclosure").first
        if disclosure.get_attribute("aria-expanded") == "false":
            disclosure.click()
        heights = page.locator(".session-link:visible").evaluate_all(
            "(rows)=>rows.map(r=>r.getBoundingClientRect().height)"
        )
        print(width, heights)
        assert max(heights) <= 52, heights
        page.evaluate(
            """()=>{state.runs[0].display_title='Long session title '+ 'long_identifier_'.repeat(16);state.runs[0].children[0].agent_label='Unicode 工作 '+ 'child_name_'.repeat(16);renderSidebar()}"""
        )
        titles = page.locator(".session-link-title:visible").evaluate_all(
            "(rows)=>rows.map(r=>r.getBoundingClientRect().height)"
        )
        assert max(titles) <= 20, titles
        row = page.locator(".session-link").first
        assert len(row.get_attribute("aria-label")) > 200
        # Keyboard expansion of full text, then restore ordinary row geometry.
        row.focus()
        page.keyboard.press("ArrowRight")
        assert (
            row.locator(".session-link-title").evaluate(
                "(e)=>getComputedStyle(e).whiteSpace"
            )
            == "normal"
        )
        page.locator(".session-move").first.click()
        expect(page.locator("#session-actions")).to_be_visible()
        bounds = page.locator("#session-actions").bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
        page.keyboard.press("Escape")
        expect(page.locator("#session-actions")).not_to_be_visible()
        page.evaluate(
            """()=>{state.runs[0].pr_summary={label:'Review PR',open:999999,merged:999999,closed:999999,unknown:999999};state.runs[0].slack_connected=true;renderSidebar()}"""
        )
        for count in page.locator(".session-pr-count").all():
            assert count.evaluate("(e)=>e.scrollWidth<=e.clientWidth+1")
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        # Selecting a child and polling titles preserve selection/disclosure.
        child = page.locator(".child-session").first
        child.click()
        expect(child).to_have_attribute("aria-current", "page")
        if width < 850:
            page.get_by_role("button", name="Open sidebar", exact=True).click()
        page.locator("#fixture-poll").click()
        expect(child).to_have_attribute("aria-current", "page")
        page.emulate_media(reduced_motion="reduce")
        expect(page.locator(".session-spinner").first).to_have_css(
            "animation-name", "none"
        )
    assert not errors, errors
    print(
        "PASS: geometry, long titles, full keyboard text, actions/Escape, rich counts, child navigation, polling, widths and reduced motion"
    )
    b.close()
