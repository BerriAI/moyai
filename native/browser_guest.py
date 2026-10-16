"""Structured operations on the desktop's Chromium. Imported only in the guest.

CDP is private loopback plumbing. Callers supply operations, never JavaScript,
CDP methods, debugger URLs, shell fragments, or filesystem paths.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

CDP = "http://127.0.0.1:9222"
PROFILE = "/workspace/.moyai-browser"
SOCKET_NAME = "browser.sock"
IDLE_SECONDS = 900


def endpoint_alive():
    try:
        with urlopen(f"{CDP}/json/version", timeout=1) as response:
            return bool(json.load(response).get("webSocketDebuggerUrl"))
    except (OSError, ValueError):
        return False


def connect(playwright, deadline):
    # Leave Chromium's download/focus/emulation defaults with its human owner.
    return playwright.chromium.connect_over_cdp(
        CDP, no_defaults=True, timeout=max(1, min(10000, (deadline - time.time()) * 1000)),
    )


def tab_id(context, page):
    session = context.new_cdp_session(page)
    try:
        return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        session.detach()


def page_map(context):
    return {tab_id(context, page): page for page in context.pages if not page.is_closed()}


def page_title(page):
    # page.title() waits for Playwright's isolated utility world. A BFCache
    # document restored from a previous short CDP attachment may not have that
    # attachment's world, although navigation and the main world are ready.
    value = page.evaluate("document.title")
    return value[:300] if isinstance(value, str) else ""


def tabs(pages, trace=None):
    values = []
    for identity, page in list(pages.items())[:32]:
        if page.is_closed():
            continue
        if trace:
            trace("tab_title")
        title = page_title(page)
        if trace:
            trace("tab_visibility")
        active = page.evaluate("document.visibilityState === 'visible'")
        values.append({
            "id": identity, "title": title, "url": page.url[:2048],
            # Visibility reflects a person's native-tab selection too.
            "active": active,
        })
    return {"tabs": values, "truncated": len(pages) > 32}


def move_cursor(page, x, y, env, deadline):
    """Move the real X pointer to the actual action, including browser chrome."""
    geometry = page.evaluate("""() => ({
        x: screenX + Math.max(0, (outerWidth - innerWidth) / 2),
        y: screenY + Math.max(0, outerHeight - innerHeight)
    })""")
    subprocess.run(
        ["xdotool", "mousemove", str(round(geometry["x"] + x)), str(round(geometry["y"] + y))],
        env=env, check=True, timeout=max(.1, min(5, deadline - time.time())),
    )


def operate(body, deadline, env, *, diagnostics=False, resident=None):
    from playwright.sync_api import sync_playwright

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
    trace("driver_start")
    if resident is not None:
        trace("connect")
        browser = resident.connect(deadline)
        trace("connected")
        try:
            return perform(browser, body, deadline, env, trace)
        except BaseException as error:
            trace("failed:" + type(error).__name__)
            if not isinstance(error, ValueError):
                # An interrupted or failed driver call leaves the shared
                # connection in an unknown state; the next command reconnects.
                resident.drop()
            raise
    with sync_playwright() as playwright:
        trace("connect")
        browser = connect(playwright, deadline)
        trace("connected")
        try:
            return perform(browser, body, deadline, env, trace)
        except BaseException as error:
            trace("failed:" + type(error).__name__)
            raise
        finally:
            # For connect_over_cdp this disconnects the client; Chromium and its
            # persistent profile/tabs outlive this short workspace execution.
            trace("disconnect")
            browser.close()
            trace("disconnected")


def perform(browser, body, deadline, env, trace):
    """One structured command on an attached browser; the caller owns the connection."""
    action = body["action"]
    context = browser.contexts[0]
    context.set_default_timeout(max(1, min(10000, (deadline - time.time()) * 1000)))
    context.set_default_navigation_timeout(max(1, min(15000, (deadline - time.time()) * 1000)))
    trace("enumerate")
    pages = page_map(context)
    if action == "tabs":
        trace("tab_metadata")
        return tabs(pages, trace)
    identity = body.get("tab_id", "")
    if identity and identity not in pages and not (action == "open" and body.get("new_tab", True)):
        raise ValueError("This browser tab closed. Inspect the tab list before continuing.")
    page = pages.get(identity)
    if page is None and pages:
        page = next((p for p in pages.values() if p.evaluate("document.visibilityState === 'visible'")),
                    next(iter(pages.values())))
    if action == "open" and (body.get("new_tab", True) or page is None):
        if len(pages) >= 32:
            raise ValueError("Close a browser tab before opening another (32 tab limit).")
        page = context.new_page()
    if page is None:
        raise ValueError("Open a browser tab first.")
    identity = tab_id(context, page)
    if action not in {"inspect", "screenshot"}:
        trace("focus")
        page.bring_to_front()
        trace("focused")
    trace("action")
    if action == "open":
        page.goto(body.get("url") or "about:blank", wait_until="domcontentloaded")
    elif action in {"back", "forward"}:
        # History can restore a cached document without a fresh DOM
        # readiness event. A committed traversal is sufficient for the
        # browser toolbar; never retry the traversal after a timeout.
        getattr(page, {"back": "go_back", "forward": "go_forward"}[action])(wait_until="commit")
    elif action == "reload":
        page.reload(wait_until="domcontentloaded")
    elif action == "close":
        # Keep the embedded browser available when its final page closes.
        # This is a real blank Chromium tab, not another rendering surface.
        if len(pages) == 1:
            context.new_page()
        page.close()
        return tabs(page_map(context), trace)
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
            target.scroll_into_view_if_needed()
            box = target.bounding_box()
            if box is None:
                raise ValueError("The target is not visible.")
            move_cursor(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, env, deadline)
            if action == "fill":
                target.fill(body.get("text", ""))
            else:
                target.click()
        elif action == "click":
            x, y = body["x"], body["y"]
            size = page.evaluate("({width: innerWidth, height: innerHeight})")
            if not 0 <= x < size["width"] or not 0 <= y < size["height"]:
                raise ValueError("Click coordinates are outside the page viewport.")
            move_cursor(page, x, y, env, deadline)
            page.mouse.click(x, y)
        else:
            raise ValueError("Fill requires an observed selector, label, or role/name.")
    elif action == "type":
        page.keyboard.insert_text(body["text"])
    elif action == "key":
        page.keyboard.press(body["key"])
    elif action == "scroll":
        page.mouse.wheel(body.get("dx", 0), body.get("dy", 0))
    elif action == "screenshot":
        directory = Path("/workspace/.moyai/browser-captures")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid4().hex}.png"
        # Current viewport only, never an unbounded full-page capture.
        page.screenshot(path=str(path), full_page=False, timeout=10000)
        return {"tab_id": identity, "title": page_title(page), "path": str(path)}
    elif action not in {"inspect", "select"}:
        raise ValueError("Unsupported browser action.")
    trace("observe")
    result = {**tabs(page_map(context), trace), "tab_id": identity}
    if action == "inspect":
        snapshot = page.locator("body").aria_snapshot(timeout=5000)
        result.update({
            "title": page_title(page), "url": page.url[:2048],
            "snapshot": snapshot[:16000], "snapshot_truncated": len(snapshot) > 16000,
            "coordinate_space": "page viewport CSS pixels; no browser chrome",
        })
    trace("result_ready")
    return result


class Resident:
    """One Playwright driver and CDP connection reused across commands.

    Starting the driver and attaching to Chromium cost ~250 ms per command when
    repeated in every workspace execution; the resident keeps both alive and
    reattaches transparently after Chromium restarts.
    """

    def __init__(self):
        self.playwright = None
        self.browser = None

    def connect(self, deadline):
        from playwright.sync_api import sync_playwright

        if self.playwright is None:
            self.playwright = sync_playwright().start()
        if self.browser is not None and self.browser.is_connected():
            try:
                # A restarted Chromium may not have been noticed yet; one cheap
                # browser-level round trip settles the connection state first.
                self.browser.new_browser_cdp_session().detach()
            except Exception:  # noqa: BLE001 - any failure means reattach
                self.drop()
        if self.browser is None or not self.browser.is_connected():
            self.browser = connect(self.playwright, deadline)
        return self.browser

    def drop(self):
        browser, self.browser = self.browser, None
        if browser is not None:
            with contextlib.suppress(Exception):  # a dead connection has nothing to release
                browser.close()


class DriverUnavailable(ConnectionError):
    """No resident driver accepted the command; nothing was dispatched."""


def _read_line(connection, deadline):
    chunks = []
    while True:
        connection.settimeout(max(.1, deadline - time.time()))
        chunk = connection.recv(1 << 16)
        if not chunk:
            break
        chunks.append(chunk)
        if chunk.endswith(b"\n"):
            break
    return b"".join(chunks)


def request(state, command, deadline, env, *, diagnostics=False):
    """Run one command on the resident driver started by the desktop guest.

    Raises DriverUnavailable only when no request was sent, so the caller may
    fall back to a private driver. A reply lost after sending is never retried.
    """
    message = json.dumps({
        "command": command, "deadline": deadline, "diagnostics": diagnostics,
        "env": {key: env[key] for key in ("DISPLAY",) if key in env},
    }).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(.1, min(2, deadline - time.time())))
        try:
            client.connect(str(Path(state) / SOCKET_NAME))
        except OSError as error:
            raise DriverUnavailable(str(error)) from error
        client.sendall(message)
        reply = _read_line(client, deadline)
    if not reply:
        raise RuntimeError("The browser driver closed without a reply.")
    reply = json.loads(reply)
    if "error" in reply:
        kind = {"ValueError": ValueError, "TimeoutError": TimeoutError}.get(reply["error"]["type"], RuntimeError)
        raise kind(reply["error"]["message"])
    return reply["result"]


def serve(state, idle_seconds=IDLE_SECONDS):
    """Resident driver loop: one command at a time over a private unix socket.

    The desktop guest serializes callers with its command lock and sends each
    command's deadline; the same deadline arms an alarm here, so a caller that
    times out cannot leave a delayed click running past the operation fence.
    """
    state = Path(state)
    state.mkdir(mode=0o700, exist_ok=True)
    path = state / SOCKET_NAME
    path.unlink(missing_ok=True)
    resident = Resident()

    def expired(_signal, _frame):
        raise TimeoutError("The desktop action expired.")
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGALRM, expired)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(1)
        server.settimeout(idle_seconds)
        try:
            while True:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    return
                with connection:
                    handle(connection, resident)
        finally:
            resident.drop()
            path.unlink(missing_ok=True)


def handle(connection, resident):
    deadline = time.time() + 30
    try:
        payload = _read_line(connection, deadline)
        if not payload:
            return
        message = json.loads(payload)
        deadline = float(message["deadline"])
        if deadline <= time.time():
            raise TimeoutError("This desktop action expired before dispatch.")
        armed = threading.current_thread() is threading.main_thread()
        if armed:
            signal.setitimer(signal.ITIMER_REAL, deadline - time.time())
        try:
            reply = {"result": operate(
                message["command"], deadline, {**os.environ, **message.get("env", {})},
                diagnostics=bool(message.get("diagnostics")), resident=resident,
            )}
        finally:
            if armed:
                signal.setitimer(signal.ITIMER_REAL, 0)
    except Exception as error:  # noqa: BLE001 - the guest sanitizes at its own boundary
        reply = {"error": {"type": type(error).__name__, "message": str(error)[:300]}}
    try:
        connection.sendall(json.dumps(reply).encode() + b"\n")
    except OSError:
        pass  # The caller gave up; its deadline already interrupted this command.


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["serve"]:
        serve(sys.argv[2] if len(sys.argv) > 2 else "/tmp/moyai-native-desktop")
