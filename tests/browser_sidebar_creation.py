"""Sidebar creation affordances against the real UI with synthetic API fixtures.

Run: python tests/browser_sidebar_creation.py (requires Playwright, Pillow and Chromium).
Set CHROMIUM_EXECUTABLE to use a system browser, or install Playwright Chromium.
The fixture is local-only and stopped when the checks finish; no backend writes.
"""
import os
from io import BytesIO
from pathlib import Path
import subprocess
import time
import urllib.request

from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8842"


def check_search_caret(page):
    page.locator("#search-sessions").click()
    field = page.locator("#session-search")
    heights = {}
    for state, value, key in (
        ("empty", "", "Home"),
        ("end", "abc", "End"),
        ("home", "abc", "Home"),
        ("middle", "abc", "ArrowLeft"),
        ("long", "a" * 100, "End"),
        ("cleared", "", "Home"),
    ):
        field.fill(value)
        field.press(key)
        frames = []
        for _ in range(7):
            frames.append(Image.open(BytesIO(field.screenshot(caret="initial"))).convert("RGB"))
            page.wait_for_timeout(180)
        bounds = [ImageChops.difference(frames[0], frame).getbbox() for frame in frames[1:]]
        visible = [box for box in bounds if box]
        assert visible, f"No blinking caret observed: {state}"
        assert all(box[2] - box[0] <= 2 for box in visible), visible
        heights[state] = max(box[3] - box[1] for box in visible)
    assert len(set(heights.values())) == 1, f"Clipped search caret: {heights}"
    print(f"PASS: caret heights {heights}")
    field.press("Escape")


def checks():
    with sync_playwright() as p:
        options = {"headless": True, "args": ["--no-sandbox"]}
        if os.environ.get("CHROMIUM_EXECUTABLE"):
            options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
        browser = p.chromium.launch(**options)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors, writes = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: writes.append(request.url)
                    if request.method == "POST" and request.url.endswith(("/api/runs", "/api/session-folders")) else None)
            page.goto(BASE)
            prompt = page.get_by_label("Message Moyai", exact=True)
            expect(prompt).to_be_visible()
            prompt.fill("Keep my unsent session draft")
            folder = page.get_by_role("button", name="Add folder", exact=True)
            new_session = page.locator("#new-task")
            dialog = page.get_by_role("dialog", name="New folder", exact=True)
            expect(new_session).to_contain_text("New session")
            expect(new_session.locator(".icon-plus")).to_be_visible()
            expect(folder.locator(".icon-folder-plus")).to_be_visible()
            expect(folder.locator(".icon-plus")).to_have_count(0)

            for width in (1440, 1280, 768, 320):
                page.set_viewport_size({"width": width, "height": 900})
                if width <= 850:
                    page.get_by_role("button", name="Open sidebar", exact=True).click()
                check_search_caret(page)
                for scope in ("mine", "all"):
                    page.get_by_label("Filter sessions", exact=True).select_option(scope)
                    expect(folder).to_be_visible()
                    assert folder.evaluate("node => node.scrollWidth <= node.clientWidth"), f"Clipped label at {width}px"
                    box = folder.bounding_box()
                    assert box["x"] >= 0 and box["x"] + box["width"] <= width
                    assert box["height"] >= (44 if width <= 850 else 36)
                    count_box = page.locator("#task-count").bounding_box()
                    assert box["y"] <= count_box["y"] < box["y"] + box["height"], f"Unexpected header wrapping at {width}px"
                folder.focus()
                page.keyboard.press("Shift+Tab")
                page.keyboard.press("Tab")
                expect(folder).to_be_focused()
                assert folder.evaluate("node => node.matches(':focus-visible')")
                folder.press("Enter")
                expect(dialog).to_be_visible()
                expect(page.get_by_label("Folder name", exact=True)).to_be_focused()
                page.keyboard.press("Escape")
                expect(dialog).not_to_be_visible()
                expect(folder).to_be_focused()
                folder.click()
                dialog.get_by_role("button", name="Cancel", exact=True).click()
                expect(dialog).not_to_be_visible()
                expect(folder).to_be_focused()
                new_session.click()
                expect(prompt).to_be_focused()
                expect(prompt).to_have_text("Keep my unsent session draft")
                expect(dialog).not_to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                print(f"PASS: distinct visible actions, My/All scope, keyboard/cancel, draft and layout at {width}px")

            page.set_viewport_size({"width": 1440, "height": 900})
            page.emulate_media(reduced_motion="reduce")
            page.locator("[data-run]").first.click()
            expect(page.locator("#page-title")).not_to_have_text("New session")
            new_session.click()
            expect(prompt).to_be_focused()
            expect(dialog).not_to_be_visible()
            page.locator("[data-run]").first.click()
            page.keyboard.press("Control+k")
            expect(prompt).to_be_focused()
            expect(dialog).not_to_be_visible()
            page.get_by_role("button", name="Settings", exact=True).click()
            expect(folder).not_to_be_visible()
            expect(new_session).not_to_be_visible()
            assert not errors, errors
            assert not writes, writes
            print("PASS: new-session click and shortcut return to composer; controls hidden in Settings; no accidental writes or JS errors")
        finally:
            browser.close()


def main():
    process = subprocess.Popen(["node", "scripts/settings_ui_preview.cjs", "--port", "8842"], cwd=ROOT)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("UI fixture exited")
            try:
                urllib.request.urlopen(BASE, timeout=1).close()
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("UI fixture did not become ready")
        checks()
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
