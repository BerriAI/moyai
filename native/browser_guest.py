"""Structured operations on the desktop's Chromium. Imported only in the guest.

CDP is private loopback plumbing. Callers supply operations, never JavaScript,
CDP methods, debugger URLs, shell fragments, or filesystem paths.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

CDP = "http://127.0.0.1:9222"
PROFILE = "/workspace/.moyai-browser"


def endpoint_alive():
    try:
        with urlopen(f"{CDP}/json/version", timeout=1) as response:
            return bool(json.load(response).get("webSocketDebuggerUrl"))
    except (OSError, ValueError):
        return False


async def connect(playwright, deadline):
    # Leave Chromium's download/focus/emulation defaults with its human owner.
    return await playwright.chromium.connect_over_cdp(
        CDP, no_defaults=True, timeout=max(1, min(10000, (deadline - time.time()) * 1000)),
    )


async def tab_id(context, page):
    session = await context.new_cdp_session(page)
    try:
        return (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
    finally:
        await session.detach()


async def page_map(context):
    return {await tab_id(context, page): page for page in context.pages if not page.is_closed()}


async def page_title(page):
    # page.title() waits for Playwright's isolated utility world. A BFCache
    # document restored from a previous short CDP attachment may not have that
    # attachment's world, although navigation and the main world are ready.
    value = await page.evaluate("document.title")
    return value[:300] if isinstance(value, str) else ""


async def tabs(pages, trace=None):
    values = []
    for identity, page in list(pages.items())[:32]:
        if page.is_closed():
            continue
        if trace:
            trace("tab_title")
        title = await page_title(page)
        if trace:
            trace("tab_visibility")
        active = await page.evaluate("document.visibilityState === 'visible'")
        values.append({
            "id": identity, "title": title, "url": page.url[:2048],
            # Visibility reflects a person's native-tab selection too.
            "active": active,
        })
    return {"tabs": values, "truncated": len(pages) > 32}


async def move_cursor(page, x, y, env, deadline):
    """Move the real X pointer to the actual action, including browser chrome."""
    geometry = await page.evaluate("""() => ({
        x: screenX + Math.max(0, (outerWidth - innerWidth) / 2),
        y: screenY + Math.max(0, outerHeight - innerHeight)
    })""")
    await asyncio.to_thread(
        subprocess.run,
        ["xdotool", "mousemove", str(round(geometry["x"] + x)), str(round(geometry["y"] + y))],
        env=env, check=True, timeout=max(.1, min(5, deadline - time.time())),
    )


def operate(body, deadline, env, *, diagnostics=False):
    """One-shot compatibility entrypoint; the production worker reuses its attachment."""
    return asyncio.run(operate_async(body, deadline, env, diagnostics=diagnostics))


async def operate_async(body, deadline, env, *, diagnostics=False, connection=None):
    from playwright.async_api import async_playwright

    action = body["action"]
    operation = uuid4().hex if diagnostics else ""
    def trace(phase):
        if not diagnostics:
            return
        # DEBUG-only, bounded, guest-local operational evidence. Never record
        # URLs, selectors, page contents, pasted text, cookies or credentials.
        try:
            path = Path("/tmp/moyai-native-desktop/browser-phases.jsonl")
            if path.exists() and path.stat().st_size > 64 * 1024:
                path.write_text("")
            with path.open("a") as handle:
                handle.write(json.dumps({
                    "operation": operation, "action": action, "phase": phase, "time": time.time(),
                }) + "\n")
        except OSError:
            pass
    if connection is not None:
        trace("reuse")
        return await perform(connection, body, deadline, env, trace)
    trace("driver_start")
    async with async_playwright() as playwright:
        trace("connect")
        browser = await connect(playwright, deadline)
        trace("connected")
        try:
            return await perform(browser, body, deadline, env, trace)
        except BaseException as error:
            trace("failed:" + type(error).__name__)
            raise
        finally:
            # For connect_over_cdp this disconnects the client; Chromium and its
            # persistent profile/tabs outlive this short workspace execution.
            trace("disconnect")
            await browser.close()
            trace("disconnected")


async def perform(browser, body, deadline, env, trace):
    """One structured command on an attached browser; the caller owns the connection."""
    action = body["action"]
    context = browser.contexts[0]
    context.set_default_timeout(max(1, min(10000, (deadline - time.time()) * 1000)))
    context.set_default_navigation_timeout(max(1, min(15000, (deadline - time.time()) * 1000)))
    trace("enumerate")
    pages = await page_map(context)
    if action == "tabs":
        trace("tab_metadata")
        return await tabs(pages, trace)
    identity = body.get("tab_id", "")
    if identity and identity not in pages and not (action == "open" and body.get("new_tab", True)):
        raise ValueError("This browser tab closed. Inspect the tab list before continuing.")
    page = pages.get(identity)
    if page is None and pages:
        page = next(iter(pages.values()))
        for candidate in pages.values():
            if await candidate.evaluate("document.visibilityState === 'visible'"):
                page = candidate
                break
    if action == "open" and (body.get("new_tab", True) or page is None):
        if len(pages) >= 32:
            raise ValueError("Close a browser tab before opening another (32 tab limit).")
        page = await context.new_page()
    if page is None:
        raise ValueError("Open a browser tab first.")
    identity = await tab_id(context, page)
    if action not in {"inspect", "screenshot"}:
        trace("focus")
        await page.bring_to_front()
        trace("focused")
    trace("action")
    if action == "open":
        await page.goto(body.get("url") or "about:blank", wait_until="domcontentloaded")
    elif action in {"back", "forward"}:
        # History can restore a cached document without a fresh DOM
        # readiness event. A committed traversal is sufficient for the
        # browser toolbar; never retry the traversal after a timeout.
        await getattr(page, {"back": "go_back", "forward": "go_forward"}[action])(wait_until="commit")
    elif action == "reload":
        await page.reload(wait_until="domcontentloaded")
    elif action == "close":
        # Keep the embedded browser available when its final page closes.
        # This is a real blank Chromium tab, not another rendering surface.
        if len(pages) == 1:
            await context.new_page()
        await page.close()
        return await tabs(await page_map(context), trace)
    elif action in {"click", "fill"}:
        if body.get("selector"):
            target = page.locator(body["selector"])
        elif body.get("role"):
            target = page.get_by_role(body["role"], name=body.get("name", ""), exact=True)
        elif body.get("label"):
            target = page.get_by_label(body["label"], exact=True)
        else:
            target = None
        if target is not None:
            # Strict locators refuse ambiguous matches. Never silently
            # pick the first account/button in a page.
            await target.scroll_into_view_if_needed()
            box = await target.bounding_box()
            if box is None:
                raise ValueError("The target is not visible.")
            await move_cursor(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, env, deadline)
            if action == "fill":
                await target.fill(body.get("text", ""))
            else:
                await target.click()
        elif action == "click":
            x, y = body["x"], body["y"]
            size = await page.evaluate("({width: innerWidth, height: innerHeight})")
            if not 0 <= x < size["width"] or not 0 <= y < size["height"]:
                raise ValueError("Click coordinates are outside the page viewport.")
            await move_cursor(page, x, y, env, deadline)
            await page.mouse.click(x, y)
        else:
            raise ValueError("Fill requires an observed selector, label, or role/name.")
    elif action == "type":
        await page.keyboard.insert_text(body["text"])
    elif action == "key":
        await page.keyboard.press(body["key"])
    elif action == "scroll":
        await page.mouse.wheel(body.get("dx", 0), body.get("dy", 0))
    elif action == "screenshot":
        directory = Path("/workspace/.moyai/browser-captures")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid4().hex}.png"
        # Current viewport only, never an unbounded full-page capture.
        await page.screenshot(path=str(path), full_page=False, timeout=10000)
        return {"tab_id": identity, "title": await page_title(page), "path": str(path)}
    elif action not in {"inspect", "select"}:
        raise ValueError("Unsupported browser action.")
    trace("observe")
    result = {**(await tabs(await page_map(context), trace)), "tab_id": identity}
    if action == "inspect":
        snapshot = await page.locator("body").aria_snapshot(timeout=5000)
        result.update({
            "title": await page_title(page), "url": page.url[:2048],
            "snapshot": snapshot[:16000], "snapshot_truncated": len(snapshot) > 16000,
            "coordinate_space": "page viewport CSS pixels; no browser chrome",
        })
    trace("result_ready")
    return result


async def worker() -> None:
    """Keep the attachment warm; the separate supervisor owns all teardown."""
    import os
    import sys

    from playwright.async_api import Error, async_playwright

    playwright = await async_playwright().start()
    browser = None
    reader = asyncio.StreamReader(limit=2 * 1024 * 1024)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    async for line in reader:
        try:
            message = json.loads(line)
            deadline = float(message["deadline"])
            if deadline <= time.time():
                raise TimeoutError("The browser request expired before dispatch.")
            if browser is not None and browser.is_connected():
                try:
                    session = await browser.new_browser_cdp_session()
                    await session.detach()
                except Error:  # A closed Chromium connection can be replaced before any action.
                    browser = None
            if browser is None or not browser.is_connected():
                browser = await connect(playwright, deadline)
            result = await operate_async(
                message["command"], deadline, os.environ,
                diagnostics=bool(message.get("diagnostics")), connection=browser,
            )
            print(json.dumps({"result": result}), flush=True)
        except (Error, OSError, ValueError, TypeError, RuntimeError):
            print(json.dumps({"error": True}), flush=True)
            return


if __name__ == "__main__":
    import os
    import sys

    if sys.argv[1:] == ["worker"]:
        asyncio.run(worker())
        os._exit(0)
