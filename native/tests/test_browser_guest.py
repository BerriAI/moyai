"""Guest adapter unit doubles only: never launches Chromium or a local server."""
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("browser_guest", Path(__file__).parents[1] / "browser_guest.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class Page:
    def __init__(self, identity, *, active=False):
        self.identity, self.active = identity, active
        self.url = f"https://example.com/{identity}"
        self.calls = []
        self.closed = False
        self.mouse = SimpleNamespace(click=lambda x, y: self.calls.append(("click", x, y)),
                                     wheel=lambda x, y: self.calls.append(("scroll", x, y)))
        self.keyboard = SimpleNamespace(insert_text=lambda text: self.calls.append(("text", text)),
                                        press=lambda key: self.calls.append(("key", key)))
    def is_closed(self):
        return self.closed
    def title(self):
        return self.identity
    def evaluate(self, expression):
        if expression == "document.title":
            return self.identity
        if "visibilityState" in expression:
            return self.active
        if "innerWidth" in expression and "screenX" not in expression:
            return {"width": 1200, "height": 700}
        return {"x": 10, "y": 90}
    def bring_to_front(self):
        self.calls.append(("focus",))
    def close(self):
        self.closed = True
    def goto(self, url, **kwargs):
        self.url = url
        self.calls.append(("navigate", url))
    def locator(self, selector):
        self.calls.append(("locator", selector))
        return SimpleNamespace(
            aria_snapshot=lambda **_: "- heading Example\n" * 2000,
            scroll_into_view_if_needed=lambda: self.calls.append(("scroll-target",)),
            bounding_box=lambda: {"x": 20, "y": 30, "width": 100, "height": 40},
            click=lambda: self.calls.append(("target-click",)),
            fill=lambda text: self.calls.append(("fill", text)),
        )


@pytest.fixture
def browser(monkeypatch):
    first, second = Page("target-one"), Page("target-two", active=True)
    calls = []
    pages = [first, second]
    class Context:
        def __init__(self):
            self.pages = pages
        def new_cdp_session(self, page):
            return SimpleNamespace(
                send=lambda method: {"targetInfo": {"targetId": page.identity}},
                detach=lambda: None,
            )
        def set_default_timeout(self, *_):
            pass
        def set_default_navigation_timeout(self, *_):
            pass
        def new_page(self):
            page = Page("target-new", active=True)
            pages.append(page)
            return page
    connection = SimpleNamespace(contexts=[Context()], close=lambda: calls.append(("disconnect",)))
    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(
                connect_over_cdp=lambda endpoint, **kwargs: (calls.append((endpoint, kwargs)), connection)[1],
            ))
        def __exit__(self, *_):
            pass
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=Playwright))
    monkeypatch.setattr(guest.subprocess, "run", lambda argv, **kwargs: calls.append(tuple(argv)))
    return first, second, pages, calls


def test_inspection_tracks_actual_visible_tab_without_focus_or_input(browser):
    first, second, _, calls = browser
    result = guest.operate({"action": "inspect"}, time.time() + 25, {})
    assert result["tab_id"] == second.identity
    assert len(result["snapshot"]) == 16000 and result["snapshot_truncated"]
    assert first.calls == [] and second.calls == [("locator", "body")]
    assert calls[0][0] == "http://127.0.0.1:9222" and calls[0][1]["no_defaults"]
    assert calls[-1] == ("disconnect",)
    assert not first.closed and not second.closed


def test_stable_tab_selection_and_navigation_share_one_context(browser):
    first, second, pages, calls = browser
    result = guest.operate({"action": "open", "url": "http://localhost:3000", "new_tab": True}, time.time() + 25, {})
    assert result["tab_id"] == "target-new" and len(pages) == 3
    assert first.calls == second.calls == []
    assert pages[-1].calls == [("focus",), ("navigate", "http://localhost:3000")]
    guest.operate({"action": "select", "tab_id": first.identity}, time.time() + 25, {})
    assert first.calls == [("focus",)]
    assert calls.count(("disconnect",)) == 2


@pytest.mark.parametrize("action,method", [("back", "go_back"), ("forward", "go_forward")])
def test_history_returns_after_document_commit_without_waiting_for_new_dom_event(browser, action, method):
    _, page, _, _ = browser
    traversals = []
    def traverse(*, wait_until):
        traversals.append(wait_until)
        if wait_until != "commit":
            raise TimeoutError("A restored document did not emit another DOMContentLoaded event.")
        page.url = f"https://example.test/{action}"
    def stale_utility_world():
        raise AssertionError("A restored page must not wait for a previous CDP attachment's utility world.")
    page.title = stale_utility_world
    setattr(page, method, traverse)
    result = guest.operate({"action": action, "tab_id": page.identity}, time.time() + 25, {})
    assert traversals == ["commit"]
    assert next(tab for tab in result["tabs"] if tab["id"] == page.identity)["url"] == page.url


def test_unknown_history_traversal_is_not_repeated(browser):
    _, page, _, _ = browser
    traversals = []
    def uncertain(*, wait_until):
        traversals.append(wait_until)
        raise TimeoutError("Traversal may have occurred.")
    page.go_back = uncertain
    with pytest.raises(TimeoutError):
        guest.operate({"action": "back", "tab_id": page.identity}, time.time() + 25, {})
    assert traversals == ["commit"]


def test_action_cursor_uses_actual_observed_target_position(browser):
    _, second, _, calls = browser
    guest.operate({"action": "click", "selector": "#observed"}, time.time() + 25, {"DISPLAY": ":99"})
    assert ("xdotool", "mousemove", "80", "140") in calls
    assert second.calls == [("focus",), ("locator", "#observed"), ("scroll-target",), ("target-click",)]


def test_stale_tab_does_not_fall_back_to_another_page(browser):
    first, second, _, calls = browser
    with pytest.raises(ValueError, match="closed"):
        guest.operate({"action": "click", "tab_id": "gone", "x": 50, "y": 50}, time.time() + 25, {})
    assert first.calls == second.calls == []
    assert calls[-1] == ("disconnect",)


def test_coordinate_click_refuses_outside_the_current_viewport(browser):
    with pytest.raises(ValueError, match="outside"):
        guest.operate({"action": "click", "x": 1270, "y": 20}, time.time() + 25, {})
    assert not any(call[0] == "xdotool" for call in browser[-1])


def test_closing_last_tab_keeps_a_real_blank_page_and_the_browser_available(browser):
    first, second, pages, _ = browser
    pages.remove(first)
    result = guest.operate({"action": "close", "tab_id": second.identity}, time.time() + 25, {})
    assert second.closed
    assert [tab["id"] for tab in result["tabs"]] == ["target-new"]
    assert not pages[-1].closed


def test_new_tab_does_not_depend_on_a_previously_closed_target(browser):
    result = guest.operate(
        {"action": "open", "tab_id": "closed", "new_tab": True, "url": "https://example.org"},
        time.time() + 25, {},
    )
    assert result["tab_id"] == "target-new"
