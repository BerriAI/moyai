"""Guest-only desktop helper, invoked through the native Modal workspace API.

Raw VNC is loopback-only. The broker reaches the WebSocket bridges through
server-held Modal connect tokens. Commands are argv arrays, never shell input.
"""
from __future__ import annotations

import base64
import ctypes
import fcntl
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

WIDTH, HEIGHT = 1280, 800
DISPLAY = ":99"
STATE = Path("/tmp/moyai-native-desktop")
ENV = {**os.environ, "DISPLAY": DISPLAY}
KEYS = {
    "Enter": "Return", "Tab": "Tab", "Escape": "Escape", "Backspace": "BackSpace",
    "Delete": "Delete", "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left",
    "ArrowRight": "Right", "Home": "Home", "End": "End", "PageUp": "Prior",
    "PageDown": "Next", " ": "space",
    "+": "plus", "-": "minus", "=": "equal", "/": "slash", "?": "question",
    ",": "comma", ".": "period", "[": "bracketleft", "]": "bracketright",
    **{f"F{index}": f"F{index}" for index in range(1, 13)},
}


def display_alive():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(.2)
            probe.connect("/tmp/.X11-unix/X99")
        return True
    except OSError:
        return False


def launch(*command):
    return subprocess.Popen(command, env=ENV, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remaining(deadline, limit):
    duration = min(limit, deadline - time.time())
    if duration <= 0:
        raise TimeoutError("This desktop action expired before dispatch.")
    return duration


def browser(url="about:blank"):
    return launch("chromium", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                  "--user-data-dir=/workspace/.moyai-browser", "--start-maximized",
                  "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9222",
                  "--restore-last-session",
                  "--window-position=0,0", f"--window-size={WIDTH},{HEIGHT - 32}", url)


def terminal():
    return launch("xterm", "-geometry", "110x30+60+60", "-bg", "#0b1627", "-fg", "#e7effa",
                  "-fa", "DejaVu Sans Mono", "-fs", "12", "-title", "Moyai workspace",
                  "-e", "bash", "-lc", "cd /workspace && exec bash")


def ensure_process(name, *command):
    pid_file = STATE / f"{name}.pid"
    try:
        os.kill(int(pid_file.read_text()), 0)
    except (OSError, ValueError):
        pid_file.write_text(str(launch(*command).pid))


def ensure_stream_process(name, deadline, *command):
    """Reconfigure only this component's daemon; do not kill a reused PID."""
    pid_file = STATE / f"{name}.pid"
    try:
        pid = int(pid_file.read_text())
        process = Path(f"/proc/{pid}")
        running = tuple(part.decode() for part in (process / "cmdline").read_bytes().split(b"\0") if part)
        if running == command:
            return
        if running:
            # Both command prefixes include the display/listen port, in addition
            # to the executable. A stale PID must not target an unrelated app.
            if running[:6] != command[:6]:
                raise RuntimeError("The desktop stream process changed identity.")
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                remaining(deadline, 1)
                if not process.exists() or (process / "stat").read_text().split()[2] == "Z":
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("The previous desktop stream did not stop.")
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass
    pid_file.write_text(str(launch(*command).pid))


def release_vnc_input():
    """Synchronous x11vnc gone hook, only for the exclusive initialized client.

    -accept rejects overlap before x11vnc installs the rejected client's gone
    hook. -nothreads makes this finish before another client can send input.
    Handshake probes never injected input and must not clear another tool's keys.
    Do not call this from the broker: its delayed finalizer can outlive a newer
    controller. Do not acquire command.lock: startup can be waiting on x11vnc.
    """
    if (os.environ.get("RFB_MODE") != "gone" or os.environ.get("RFB_CLIENT_COUNT") != "0"
            or os.environ.get("RFB_STATE") != "NORMAL"):
        return
    # XTest is already required by x11vnc/xdotool. Use actual X key state so
    # modifiers and ordinary keys are released without an RFB parser or ledger.
    x11, xtest = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], ctypes.c_void_p
    x11.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    x11.XGetPointerMapping.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    for function in (xtest.XTestFakeKeyEvent, xtest.XTestFakeButtonEvent):
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(DISPLAY.encode())
    if not display:
        raise RuntimeError("The desktop display is unavailable for input release.")
    try:
        keys = (ctypes.c_ubyte * 32)()
        x11.XQueryKeymap(display, keys)
        for code in range(8, 256):
            if keys[code // 8] & (1 << (code % 8)):
                xtest.XTestFakeKeyEvent(display, code, 0, 0)
        # XQueryPointer reports only buttons 1-5. Release every physical button,
        # including side buttons, without moving the pointer or toggling locks.
        mapping = (ctypes.c_ubyte * 256)()
        for button in range(1, x11.XGetPointerMapping(display, mapping, len(mapping)) + 1):
            xtest.XTestFakeButtonEvent(display, button, 0, 0)
        # Flush and wait for X to apply releases before the hook returns.
        x11.XSync(display, 0)
    finally:
        x11.XCloseDisplay(display)


def streaming(deadline):
    # The viewer has its own server-enforced read-only endpoint. A browser
    # cannot turn viewing into control by changing noVNC's viewOnly flag.
    for name, port, sharing, extra in [
        ("vnc-control", 5900, ["-nevershared", "-dontdisconnect"], [
            "-norc", "-nothreads",
            # LibVNCServer's exclusivity check runs after the RFB handshake.
            # Reject earlier, before installing a gone hook on a second client.
            "-accept", 'test "$RFB_CLIENT_COUNT" = 0',
            "-gone", shlex.join([sys.executable, str(Path(__file__).resolve()), "--vnc-disconnect"]),
        ]),
        # noVNC renders cursor shapes locally but does not follow an agent's
        # CursorPos updates. Composite the real X pointer into viewer pixels.
        # Keep the controller's locally rendered cursor responsive.
        ("vnc-view", 5901, ["-shared"], [
            "-viewonly", "-nosetclipboard", "-nosetprimary", "-nocursorshape", "-nocursorpos",
        ]),
    ]:
        ensure_stream_process(name, deadline, "x11vnc", "-display", DISPLAY, "-localhost", "-rfbport", str(port),
                              "-forever", *sharing, "-nopw", "-safer", "-wait", "10", "-defer", "5", "-quiet", *extra)
    for name, public_port, private_port in [
        ("ws-control", 6080, 5900), ("ws-view", 6081, 5901),
    ]:
        ensure_stream_process(name, deadline, sys.executable, "-m", "websockify", "--heartbeat", "30",
                              f"0.0.0.0:{public_port}", f"127.0.0.1:{private_port}")
    for port in (5900, 5901, 6080, 6081):
        for _ in range(100):
            remaining(deadline, 1)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("The desktop stream did not start.")


def start(deadline):
    packages = {
        "Xvfb": "xvfb", "openbox": "openbox", "xterm": "xterm", "xdotool": "xdotool",
        "chromium": "chromium", "xsetroot": "x11-xserver-utils", "tint2": "tint2", "x11vnc": "x11vnc",
    }
    missing = [package for command, package in packages.items() if not shutil.which(command)]
    if missing:
        for command in [
            ["apt-get", "update", "-qq"],
            ["apt-get", "install", "-y", "-qq", "--no-install-recommends", *missing, "fonts-dejavu-core"],
        ]:
            subprocess.run(command, check=True, timeout=remaining(deadline, 240),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import PIL  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "pillow==12.3.0"],
                       check=True, timeout=remaining(deadline, 120),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import websockify  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "websockify==0.13.0"],
                       check=True, timeout=remaining(deadline, 120),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import playwright  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "playwright==1.63.0"],
                       check=True, timeout=remaining(deadline, 120),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not display_alive():
        # Stale locks occur after filesystem snapshots; never remove live locks.
        Path("/tmp/.X99-lock").unlink(missing_ok=True)
        launch("Xvfb", DISPLAY, "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp")
        for _ in range(50):
            remaining(deadline, 1)
            if display_alive():
                break
            time.sleep(.1)
        else:
            raise RuntimeError("The desktop display did not start.")
    ensure_process("openbox", "openbox", "--sm-disable")
    subprocess.run(["xsetroot", "-solid", "#0b1627"], env=ENV, check=True, timeout=remaining(deadline, 10))
    # A window switcher keeps Chromium and terminals accessible as ordinary
    # desktop windows. Nothing is exposed outside the authenticated broker.
    panel = STATE / "tint2rc"
    panel.write_text(
        "panel_items = LTSC\npanel_position = bottom center horizontal\n"
        "panel_size = 100% 32\npanel_background_id = 0\n"
        "taskbar_mode = single_desktop\nlauncher_icon_size = 22\n"
        "launcher_item_app = /usr/share/applications/chromium.desktop\n"
        "launcher_item_app = /usr/share/applications/debian-xterm.desktop\n"
        "time1_format = %H:%M\n"
    )
    ensure_process("panel", "tint2", "-c", str(panel))
    # Reopening a running desktop preserves the existing browser tabs/profile.
    def windows():
        result = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "chromium"],
                                env=ENV, check=False, capture_output=True, text=True, timeout=remaining(deadline, 5))
        return result.stdout.split()

    # Upgrade only the Chromium parent owning our exact persistent profile.
    # Graceful close allows --restore-last-session to retain its tabs. Never
    # terminate an unrelated browser or a process from a stale PID file.
    if not browser_runtime.endpoint_alive():
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                args = (process / "cmdline").read_bytes().split(b"\0")
                if (b"--user-data-dir=/workspace/.moyai-browser" in args
                        and not any(arg.startswith(b"--type=") for arg in args)
                        and args and Path(os.fsdecode(args[0])).name == "chromium"):
                    os.kill(int(process.name), signal.SIGTERM)
                    for _ in range(100):
                        remaining(deadline, 1)
                        if not process.exists() or (process / "stat").read_text().split()[2] == "Z":
                            break
                        time.sleep(.1)
                    else:
                        raise RuntimeError("The existing browser did not close for its update.")
            except (FileNotFoundError, ProcessLookupError):
                pass
    existing = windows()
    if not existing:
        browser()
        for _ in range(100):
            remaining(deadline, 1)
            existing = windows()
            if existing:
                break
            time.sleep(.1)
        else:
            raise RuntimeError("The browser did not open.")
    subprocess.run(["xdotool", "windowactivate", "--sync", existing[-1]],
                   env=ENV, check=True, timeout=remaining(deadline, 5))
    for _ in range(100):
        remaining(deadline, 1)
        if browser_runtime.endpoint_alive():
            break
        time.sleep(.1)
    else:
        raise RuntimeError("The shared browser did not start.")
    streaming(deadline)


def screenshot():
    from PIL import ImageGrab
    image = ImageGrab.grab(xdisplay=DISPLAY)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=72)
    return base64.b64encode(output.getvalue()).decode()


def command(body):
    global browser_runtime
    action = body["action"]
    deadline = float(body.get("deadline", time.time() + 600))
    # Kill delayed/abandoned work before its database operation fence expires.
    def expired(_signal, _frame):
        raise TimeoutError("The desktop action expired.")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining(deadline, 600))
    helper = body.get("browser_helper", str(Path(__file__).with_name("browser_guest.py")))
    spec = importlib.util.spec_from_file_location("moyai_browser_guest", helper)
    browser_runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(browser_runtime)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "command.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A delayed private exec cannot inject input after its ownership fence
        # expires, even when its HTTP caller has already timed out.
        remaining(deadline, 1)
        if action == "start":
            start(deadline)
        if action == "browser":
            if not display_alive() or not browser_runtime.endpoint_alive():
                if body["command"]["action"] == "tabs":
                    return {"tabs": [], "truncated": False, "available": False}
                if body["command"]["action"] != "open":
                    raise ValueError("Open the shared browser first.")
                start(deadline)
            return browser_runtime.operate(
                body["command"], deadline, ENV, diagnostics=bool(body.get("browser_diagnostics")),
            )
        if not display_alive():
            return {"available": False, "width": WIDTH, "height": HEIGHT}
        if action == "open":
            browser(body.get("url") or "chrome://newtab/")
        elif action == "terminal":
            terminal()
        elif action == "click":
            # Older distro xdotool builds can wait forever on --sync when the
            # pointer is already at the requested position. X queues move/click
            # in order on this connection; no movement wait is needed.
            subprocess.run(["xdotool", "mousemove", str(body["x"]), str(body["y"]),
                            "click", str(body.get("button", 1))], env=ENV, check=True, timeout=remaining(deadline, 10))
        elif action == "type":
            subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "1", "--", body["text"]],
                           env=ENV, check=True, timeout=remaining(deadline, 20))
        elif action == "key":
            value = body["key"]
            key = KEYS.get(value, value if len(value) == 1 and value.isascii() and value.isalnum() else "")
            if not key:
                raise ValueError("Unsupported desktop key.")
            modifiers = [part for part in ("ctrl", "alt", "shift") if body.get(part)]
            subprocess.run(["xdotool", "key", "--clearmodifiers", "+".join([*modifiers, key])],
                           env=ENV, check=True, timeout=remaining(deadline, 10))
        elif action == "scroll":
            subprocess.run(["xdotool", "click", "--repeat", str(body.get("steps", 3)),
                            "4" if body["direction"] == "up" else "5"], env=ENV, check=True, timeout=remaining(deadline, 10))
        elif action not in {"start", "state", "frame"}:
            raise ValueError("Unsupported desktop action.")
        return {"available": True, "width": WIDTH, "height": HEIGHT,
                **({"frame": screenshot()} if action in {"frame", "start"} else {})}


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--vnc-disconnect"]:
            release_vnc_input()
        else:
            print(json.dumps(command(json.loads(sys.argv[1]))))
    except Exception:  # noqa: BLE001 - sanitize page/provider errors at the guest boundary
        print(json.dumps({"error": "The desktop action could not be confirmed. Check the screen before trying again."}))
        sys.exit(1)
