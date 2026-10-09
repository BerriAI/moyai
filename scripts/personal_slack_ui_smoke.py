"""Browser regression against explicitly synthetic UI fixtures, NOT Slack/backend proof.
Run settings_ui_preview.cjs on localhost:8840 first, then this with system Python
(which includes Playwright). No real credentials, OAuth, model, or Slack writes.
"""
from playwright.sync_api import sync_playwright, expect

BASE = "http://127.0.0.1:8840"
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    for width in (1440, 768, 320):
        page.set_viewport_size({"width": width, "height": 1000})
        page.emulate_media(reduced_motion="reduce")
        page.goto(BASE + "/?fixture=personal-connected#personal-slack")
        expect(page.get_by_role("heading", name="Personal Slack", exact=True)).to_be_visible()
        expect(page.get_by_text("Your personal Slack account", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.get_by_role("button", name="Disconnect…", exact=True).click()
        expect(page.get_by_role("dialog", name="Disconnect personal Slack?")).to_be_visible()
        expect(page.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name="Disconnect personal Slack?")).not_to_be_visible()
        page.get_by_role("button", name="Check connection", exact=True).click()
        expect(page.get_by_text("Connection checked.", exact=True)).to_be_visible()
        page.get_by_role("button", name="Start private web chat", exact=True).click()
        expect(page.get_by_role("checkbox", name="Private session · Only you")).to_be_checked()
        page.get_by_role("textbox", name="Message Moyai").fill("Synthetic private session smoke")
        with page.expect_request(lambda r: r.url.endswith("/api/runs") and r.method == "POST") as pending:
            page.get_by_role("button", name="Start session", exact=True).click()
        assert pending.value.post_data_json["private_session"] is True
        expect(page.locator(".session-privacy-notice")).to_contain_text("Private session · Only you")
        page.get_by_role("button", name="Run again", exact=True).click()
        expect(page.get_by_role("checkbox", name="Private session · Only you")).to_be_checked()
        expect(page.get_by_role("checkbox", name="Private session · Only you")).to_be_disabled()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        print(f"synthetic UI {width}px: settings/check/cancel/private-create/retry lock passed")
    page.goto(BASE + "/?fixture=personal-error#personal-slack")
    expect(page.get_by_text("Needs attention", exact=True)).to_be_visible()
    page.get_by_role("button", name="Check connection", exact=True).click()
    expect(page.locator("#personal-slack-error")).to_contain_text("organization fallback is not used")
    expect(page.get_by_text("Your personal Slack account", exact=True)).to_be_visible()
    page.get_by_role("button", name="Disconnect…", exact=True).click()
    page.get_by_role("button", name="Disconnect", exact=True).click()
    expect(page.get_by_text("Organization Slack connection", exact=True)).to_be_visible()
    page.get_by_role("button", name="Connect Slack", exact=True).click()
    expect(page.get_by_text("Your personal Slack account", exact=True)).to_be_visible()
    print("synthetic UI: error/no-fallback/disconnect/mock-OAuth passed")
    page.goto(BASE + "/?fixture=personal-chat#personal-slack")
    page.get_by_role("button", name="Start private web chat", exact=True).click()
    page.get_by_role("textbox", name="Message Moyai").fill("Synthetic private live-chat layout")
    page.get_by_role("button", name="Start session", exact=True).click()
    expect(page.locator(".chat-panel .session-privacy-notice")).to_be_visible()
    expect(page.get_by_role("textbox", name="Message Moyai")).to_be_visible()
    page.goto(BASE + "/?fixture=member#tasks")
    expect(page.get_by_role("checkbox", name="Private session · Only you")).not_to_be_checked()
    print("synthetic UI: private live-chat marker and ordinary default passed")
    assert not errors, errors
    browser.close()
