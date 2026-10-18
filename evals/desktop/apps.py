"""Small GTK3 desktop apps for the desktop computer-use evals.

Each app keeps its task data in process memory only and writes what the
verifier needs under /var/lib/eval, so the answer is reachable through the
GUI and not through files the agent can read.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import string
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

STATE = Path("/var/lib/eval")


def write(name: str, payload: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / name).write_text(json.dumps(payload, indent=2) + "\n")


def window(title: str) -> Gtk.Window:
    win = Gtk.Window(title=title)
    win.set_default_size(520, 360)
    win.connect("destroy", Gtk.main_quit)
    return win


def license_viewer() -> Gtk.Window:
    alphabet = string.ascii_uppercase + string.digits
    code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
    write("license_answer.json", {"sha256": hashlib.sha256(code.encode()).hexdigest()})
    win = window("License Manager")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=24)
    box.pack_start(Gtk.Label(label="Acme Analytics Pro"), False, False, 0)
    box.pack_start(Gtk.Label(label="Status: Active (seat 3 of 10)"), False, False, 0)
    box.pack_start(Gtk.Label(label="Activation code:"), False, False, 0)
    box.pack_start(Gtk.Label(label=code, selectable=True), False, False, 0)
    win.add(box)
    return win


APPS = {"license": license_viewer}

if __name__ == "__main__":
    APPS[sys.argv[1]]().show_all()
    Gtk.main()
