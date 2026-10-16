"""Guest hook unit checks; no X server, browser, or Modal resources are started."""

import ctypes
import importlib.util
import shlex
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("desktop_teardown", Path(__file__).parents[1] / "desktop.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


@pytest.fixture
def services(monkeypatch):
    commands = {}
    monkeypatch.setattr(desktop, "ensure_stream_process",
                        lambda name, deadline, *argv: commands.setdefault(name, argv))
    monkeypatch.setattr(desktop.socket, "create_connection", lambda *a, **kw: nullcontext())
    desktop.streaming(time.time() + 10)
    return commands


def test_controller_admission_rejects_overlap_and_viewer_stays_shared(services):
    control, viewer = services["vnc-control"], services["vnc-view"]
    assert "-shared" not in control
    assert all(option in control for option in ("-nevershared", "-dontdisconnect", "-nothreads", "-norc"))
    assert all(option in viewer for option in (
        "-shared", "-viewonly", "-nosetclipboard", "-nosetprimary", "-nocursorshape", "-nocursorpos",
    ))
    assert "-gone" not in viewer and "-accept" not in viewer
    for service in (control, viewer):
        assert "-localhost" in service and "-safer" in service
    # Execute the actual early-admission shell expression. Missing/malformed
    # state fails closed, and even an unfinished incumbent handshake counts.
    accept = control[control.index("-accept") + 1]
    for count in ("0", "1", "2", "", "-1", "invalid"):
        result = subprocess.run(["/bin/sh", "-c", accept], env={"RFB_CLIENT_COUNT": count}, check=False)
        assert (result.returncode == 0) == (count == "0")
    hook = control[control.index("-gone") + 1]
    assert shlex.split(hook) == [desktop.sys.executable, str(Path(desktop.__file__).resolve()), "--vnc-disconnect"]
    assert "&" not in hook  # x11vnc must wait for XSync before accepting a replacement.


@pytest.fixture
def xserver(monkeypatch):
    """Model X's physical input state and flush boundary, not the RFB protocol."""
    down_keys, down_buttons = {37, 50, 65, 200, 255}, {1, 3, 8}
    calls, queued = [], []
    display = 1 << 40

    def open_display(name):
        assert name == b":99"
        calls.append("open")
        return display

    def query_keys(connection, bitmap):
        assert connection == display
        for code in down_keys:
            bitmap[code // 8] |= 1 << (code % 8)
        return 1

    def pointer_mapping(connection, mapping, size):
        assert connection == display and size == 256
        return 10  # Includes buttons XQueryPointer's core mask cannot report.

    def release_key(connection, code, pressed, delay):
        assert connection == display and pressed == delay == 0
        calls.append(("key", code))
        queued.append((down_keys, code))
        return 1

    def release_button(connection, button, pressed, delay):
        assert connection == display and pressed == delay == 0
        calls.append(("button", button))
        queued.append((down_buttons, button))
        return 1

    def sync(connection, discard):
        assert connection == display and discard == 0
        for down, value in queued:
            down.discard(value)
        queued.clear()
        calls.append("sync")

    def close_display(connection):
        assert connection == display
        calls.append("close")

    x11 = SimpleNamespace(XOpenDisplay=open_display, XQueryKeymap=query_keys, XGetPointerMapping=pointer_mapping,
                          XSync=sync, XCloseDisplay=close_display)
    xtest = SimpleNamespace(XTestFakeKeyEvent=release_key, XTestFakeButtonEvent=release_button)
    libraries = {"libX11.so.6": x11, "libXtst.so.6": xtest}
    monkeypatch.setattr(desktop.ctypes, "CDLL", libraries.__getitem__)
    for name, value in {"RFB_MODE": "gone", "RFB_CLIENT_COUNT": "0", "RFB_STATE": "NORMAL"}.items():
        monkeypatch.setenv(name, value)
    return SimpleNamespace(keys=down_keys, buttons=down_buttons, calls=calls, x11=x11)


def test_disconnect_releases_pressed_keys_and_all_pointer_buttons_synchronously(xserver):
    desktop.release_vnc_input()
    assert not xserver.keys and not xserver.buttons
    assert [call for call in xserver.calls if isinstance(call, tuple) and call[0] == "key"] == [
        ("key", 37), ("key", 50), ("key", 65), ("key", 200), ("key", 255),
    ]
    assert ("button", 8) in xserver.calls and ("button", 10) in xserver.calls
    assert xserver.calls[-2:] == ["sync", "close"]
    # A default ctypes int return would truncate an actual 64-bit Display*.
    assert xserver.x11.XOpenDisplay.restype == ctypes.c_void_p


@pytest.mark.parametrize("name,value", [
    ("RFB_MODE", "accept"), ("RFB_MODE", ""),
    ("RFB_CLIENT_COUNT", "1"), ("RFB_CLIENT_COUNT", ""),
    ("RFB_STATE", "PROTOCOL_VERSION"), ("RFB_STATE", "SECURITY_TYPE"),
    ("RFB_STATE", "AUTHENTICATION"), ("RFB_STATE", "INITIALISATION"),
])
def test_probe_or_nonexclusive_callback_cannot_clear_input(monkeypatch, xserver, name, value):
    monkeypatch.setenv(name, value)
    desktop.release_vnc_input()
    assert xserver.keys == {37, 50, 65, 200, 255} and xserver.buttons == {1, 3, 8}
    assert xserver.calls == []


def test_release_closes_x_connection_when_query_fails(monkeypatch, xserver):
    def failed_query(*_):
        raise OSError("display unavailable")
    monkeypatch.setattr(xserver.x11, "XQueryKeymap", failed_query)
    with pytest.raises(OSError):
        desktop.release_vnc_input()
    assert xserver.calls == ["open", "close"]
