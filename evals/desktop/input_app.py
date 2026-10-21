"""Disposable GTK target for native focus/edit/scroll regression checks."""
import json
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

state_dir = Path(sys.argv[1])
state_dir.mkdir(parents=True, exist_ok=True)
win = Gtk.Window(title="Native input verification")
win.set_default_size(640, 650)
win.connect("destroy", Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
win.add(box)


def entry(name, value, editable=True):
    row = Gtk.Box(spacing=12)
    row.pack_start(Gtk.Label(label=name), False, False, 0)
    widget = Gtk.Entry(text=value)
    widget.set_editable(editable)
    widget.get_accessible().set_name(name)
    row.pack_start(widget, True, True, 0)
    box.pack_start(row, False, False, 0)
    return widget


target = entry("Target text", "abcd")
locked = entry("Read only text", "LOCKED", editable=False)
decoy = entry("Decoy text", "DO NOT EDIT")
activations = 0
keys = []


def key_pressed(_widget, event):
    keys.append(int(event.keyval))
    return False


win.connect("key-press-event", key_pressed)


def activated(_widget):
    global activations
    activations += 1


target.connect("activate", activated)
buttons = Gtk.Box(spacing=8)
box.pack_start(buttons, False, False, 0)


def prepare(selection=False):
    target.set_text("abcd")
    target.grab_focus()
    if selection:
        target.select_region(1, 3)
    else:
        target.set_position(2)


for label, callback in [
    ("Prepare caret", lambda _button: prepare()),
    ("Prepare selection", lambda _button: prepare(True)),
]:
    button = Gtk.Button(label=label)
    button.connect("clicked", callback)
    buttons.pack_start(button, True, True, 0)

notes = Gtk.TextView()
notes.get_accessible().set_name("Multiline text")
notes.get_buffer().set_text("first\nsecond")
notes.set_size_request(-1, 80)
box.pack_start(notes, False, False, 0)
scroll = Gtk.ScrolledWindow()
scroll.get_accessible().set_name("Independent scroll area")
scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
scroll.set_size_request(-1, 220)
rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
for index in range(40):
    rows.pack_start(Gtk.Label(label=f"Scroll row {index + 1}"), False, False, 0)
scroll.add(rows)
box.pack_start(scroll, True, True, 0)


def save():
    buffer = notes.get_buffer()
    state = {
        "target": target.get_text(), "decoy": decoy.get_text(), "locked": locked.get_text(),
        "caret": target.get_position(), "selection": list(target.get_selection_bounds()),
        "activations": activations,
        "keys": keys,
        "notes": buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
        "scroll": scroll.get_vadjustment().get_value(),
    }
    temporary = state_dir / "input-state.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temporary.replace(state_dir / "input-state.json")
    return True


win.show_all()
prepare()
GLib.timeout_add(100, save)
Gtk.main()
