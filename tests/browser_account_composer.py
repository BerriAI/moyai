"""Real-browser regression checks for account and composer affordances.

Optional dependencies: uv run --with playwright python tests/browser_account_composer.py
Install Chromium first: uv run --with playwright playwright install chromium
The isolated local app uses a signed test identity, not live Google credentials.
Use --serve for interactive QA; /_test/sign-in resets the test login.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE = "http://127.0.0.1:8787"
EMAIL = "morgan.long-email-address@example.test"


def serve():
    from fastapi.responses import RedirectResponse
    from app.config import Settings
    from app.main import create_app
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="account-composer-") as directory:
        app = create_app(Settings(
            _env_file=None, data_dir=Path(directory), public_url=BASE,
            google_client_id="browser-test-client", google_client_secret="browser-test-only",
            google_allowed_domains="example.test", google_admin_emails=EMAIL,
            session_titles_enabled=False, organization_name="UI regression test",
        ))

        @app.get("/_test/sign-in")
        async def sign_in():
            response = RedirectResponse("/#tasks")
            app.state.security.new_session(response, identity={
                "email": EMAIL, "name": "UI Test", "domain": "example.test", "sub": "ui-test",
            })
            return response

        # The production API marks every loopback origin local; expose the
        # hosted account controls while retaining real authentication/logout.
        @app.middleware("http")
        async def hosted_account_controls(request, call_next):
            response = await call_next(request)
            if request.url.path == "/api/session":
                from fastapi.responses import JSONResponse
                body = b"".join([chunk async for chunk in response.body_iterator])
                return JSONResponse({**json.loads(body), "local": False})
            return response

        uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")


def checks():
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE + "/_test/sign-in")
        expect(page.get_by_label("Message Moyai")).to_be_visible()
        identity = page.locator(".rail-identity")
        expect(identity).to_have_text(EMAIL)
        expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
        assert page.locator(".composer-toolbar .icon-plus").count() == 0
        expect(page.locator(".attach-button .icon-paperclip")).to_be_visible()
        expect(page.locator(".task-options .icon-sliders")).to_be_visible()

        for width in (1440, 851, 850, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            if width <= 850:
                page.get_by_role("button", name="Open sidebar", exact=True).click()
            for address in (EMAIL, "m" * 64 + "@a-very-long-organization-domain.example.test"):
                identity.evaluate("(node, address) => node.textContent = address", address)
                assert identity.evaluate("""node => {
                    const style = getComputedStyle(node), rect = node.getBoundingClientRect();
                    return style.whiteSpace === 'normal' && node.scrollWidth <= node.clientWidth
                        && node.scrollHeight <= node.clientHeight && rect.width > 200
                        && rect.bottom <= innerHeight;
                }"""), f"Identity clipped at {width}px"
            identity.evaluate("(node, address) => node.textContent = address", EMAIL)
            expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
            if width <= 850:
                page.locator("#close-sidebar").click()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            expect(page.get_by_role("button", name="Attach files")).to_be_visible()
            expect(page.get_by_label("Session setup", exact=True)).to_be_visible()
        print("PASS: full email and explicit controls at 1440, 851, 850, 390, 320px")

        page.set_viewport_size({"width": 1440, "height": 900})
        page.get_by_role("button", name="Settings", exact=True).click()
        expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
        assert page.request.get(BASE + "/api/session").json()["authenticated"]
        page.get_by_role("button", name="New session", exact=False).click()
        page.get_by_label("Message Moyai").fill("Check the composer controls")
        setup = page.get_by_label("Session setup", exact=True)
        setup.focus()
        page.keyboard.press("Enter")
        expect(page.get_by_label("GitHub repository", exact=True)).to_be_visible()
        page.get_by_label("GitHub repository", exact=True).fill("https://github.com/BerriAI/moyai")
        page.get_by_role("button", name="Settings", exact=True).click()
        page.get_by_role("button", name="New session", exact=False).click()
        expect(page.get_by_label("Message Moyai")).to_have_value("Check the composer controls")
        page.get_by_label("Session setup", exact=True).click()
        expect(page.get_by_label("GitHub repository", exact=True)).to_have_value("https://github.com/BerriAI/moyai")
        page.get_by_label("Session setup", exact=True).click()
        with page.expect_file_chooser() as chooser:
            page.get_by_role("button", name="Attach files").click()
        chooser.value.set_files({"name": "ui-check.txt", "mimeType": "text/plain", "buffer": b"Real upload regression check"})
        expect(page.locator(".draft-attachment small")).to_have_text("28 B")
        expect(page.locator(".task-options")).not_to_have_attribute("open", "")
        print("PASS: Settings preserves login; setup is keyboard accessible; drafts persist; Attach uploads a file")
        page.get_by_role("button", name="Start session", exact=True).click()
        expect(page.locator("#followup")).to_be_visible()
        expect(page.locator(".attach-button .icon-paperclip")).to_be_visible()
        expect(page.get_by_role("button", name="Preview ui-check.txt", exact=True)).to_be_visible()
        with page.expect_file_chooser() as chooser:
            page.get_by_role("button", name="Attach files").click()
        chooser.value.set_files({"name": "followup.txt", "mimeType": "text/plain", "buffer": b"Follow-up upload"})
        expect(page.get_by_role("button", name="Preview followup.txt", exact=True)).to_be_visible()
        print("PASS: uploaded attachment submitted to a real demo session; follow-up uses the same paperclip control")

        page.route("**/api/logout", lambda route: route.fulfill(status=503, json={"detail": "Sign-out test failure"}))
        page.get_by_role("button", name="Sign out", exact=True).click()
        expect(page.locator("[data-sonner-toast]")).to_have_text("Sign-out test failure")
        expect(page.get_by_role("button", name="Sign out", exact=True)).to_be_enabled()
        assert page.request.get(BASE + "/api/session").json()["authenticated"]
        page.unroute("**/api/logout")
        page.get_by_role("button", name="Sign out", exact=True).click()
        expect(page.get_by_role("button", name="Continue with Google")).to_be_visible()
        assert not page.request.get(BASE + "/api/session").json()["authenticated"]
        expect(page.locator("#logout")).to_be_hidden()
        assert not errors, errors
        print("PASS: failed logout is retryable; successful logout clears authentication; no browser JS errors")
        browser.close()


def main():
    if "--serve" in sys.argv:
        serve()
        return
    process = subprocess.Popen([sys.executable, __file__, "--serve"], cwd=ROOT, env=os.environ.copy())
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Test server exited")
            try:
                urllib.request.urlopen(BASE + "/health", timeout=1)
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("Test server did not become ready")
        checks()
    finally:
        process.terminate()
        process.wait(timeout=15)


if __name__ == "__main__":
    main()
