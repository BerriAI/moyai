"""Guest-only desktop helper, invoked through the native Modal workspace API.

There is no public VNC, HTTP or browser-debugging endpoint. Commands are argv
arrays; user text never becomes shell source.
"""
from __future__ import annotations

import base64
import fcntl
import io
import json
import os
import shutil
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


def browser(url="chrome://newtab/"):
    return launch("chromium", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                  "--user-data-dir=/workspace/.moyai-browser", "--start-maximized",
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


def start(deadline):
    packages = {
        "Xvfb": "xvfb", "openbox": "openbox", "xterm": "xterm", "xdotool": "xdotool",
        "chromium": "chromium", "xsetroot": "x11-xserver-utils", "tint2": "tint2",
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


def screenshot():
    from PIL import ImageGrab
    image = ImageGrab.grab(xdisplay=DISPLAY)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=72)
    return base64.b64encode(output.getvalue()).decode()


def command(body):
    action = body["action"]
    deadline = float(body.get("deadline", time.time() + 600))
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "command.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A delayed private exec cannot inject input after its ownership fence
        # expires, even when its HTTP caller has already timed out.
        remaining(deadline, 1)
        if action == "start":
            start(deadline)
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
        print(json.dumps(command(json.loads(sys.argv[1]))))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError):
        print(json.dumps({"error": "The desktop action could not be confirmed. Check the screen before trying again."}))
        sys.exit(1)
