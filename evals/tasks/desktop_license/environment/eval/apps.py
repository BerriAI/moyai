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


def expense_form() -> Gtk.Window:
    win = window("Expense Report")
    grid = Gtk.Grid(row_spacing=10, column_spacing=12, margin=24)
    name = Gtk.Entry()
    name.get_accessible().set_name("Employee name")
    amount = Gtk.SpinButton.new_with_range(0, 100000, 0.01)
    amount.set_digits(2)
    amount.get_accessible().set_name("Amount (USD)")
    category = Gtk.ComboBoxText()
    for item in ("Meals", "Travel", "Software", "Office supplies"):
        category.append_text(item)
    category.set_active(0)
    category.get_accessible().set_name("Category")
    reimbursable = Gtk.CheckButton(label="Reimbursable")
    status = Gtk.Label(label="Not submitted")
    for row, (label, widget) in enumerate((
        ("Employee name", name), ("Amount (USD)", amount), ("Category", category),
    )):
        grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
    grid.attach(reimbursable, 1, 3, 1, 1)
    submit = Gtk.Button(label="Submit")
    grid.attach(submit, 1, 4, 1, 1)
    grid.attach(status, 1, 5, 1, 1)

    def on_submit(_button: Gtk.Button) -> None:
        amount.update()
        write("expense_submission.json", {
            "name": name.get_text(),
            "amount": round(amount.get_value(), 2),
            "category": category.get_active_text(),
            "reimbursable": reimbursable.get_active(),
        })
        status.set_text("Submitted")

    submit.connect("clicked", on_submit)
    win.add(grid)
    return win


def preferences() -> Gtk.Window:
    win = window("Preferences")
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
    notebook = Gtk.Notebook()
    general = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
    theme = Gtk.CheckButton(label="Use dark theme")
    autosave = Gtk.CheckButton(label="Autosave documents")
    autosave.set_active(True)
    general.pack_start(theme, False, False, 0)
    general.pack_start(autosave, False, False, 0)
    advanced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
    telemetry = Gtk.CheckButton(label="Send usage statistics")
    telemetry.set_active(True)
    retries_row = Gtk.Box(spacing=8)
    retries = Gtk.SpinButton.new_with_range(0, 20, 1)
    retries.set_value(3)
    retries.get_accessible().set_name("Max retries")
    retries_row.pack_start(Gtk.Label(label="Max retries"), False, False, 0)
    retries_row.pack_start(retries, False, False, 0)
    proxy = Gtk.Entry()
    proxy.get_accessible().set_name("Proxy URL")
    proxy_row = Gtk.Box(spacing=8)
    proxy_row.pack_start(Gtk.Label(label="Proxy URL"), False, False, 0)
    proxy_row.pack_start(proxy, True, True, 0)
    for widget in (telemetry, retries_row, proxy_row):
        advanced.pack_start(widget, False, False, 0)
    notebook.append_page(general, Gtk.Label(label="General"))
    notebook.append_page(advanced, Gtk.Label(label="Advanced"))
    status = Gtk.Label(label="")
    apply = Gtk.Button(label="Apply")

    def on_apply(_button: Gtk.Button) -> None:
        retries.update()
        write("preferences_applied.json", {
            "dark_theme": theme.get_active(),
            "autosave": autosave.get_active(),
            "telemetry": telemetry.get_active(),
            "max_retries": int(retries.get_value()),
            "proxy_url": proxy.get_text(),
        })
        status.set_text("Settings applied")

    apply.connect("clicked", on_apply)
    outer.pack_start(notebook, True, True, 0)
    outer.pack_start(apply, False, False, 0)
    outer.pack_start(status, False, False, 0)
    win.add(outer)
    return win


APPS = {"license": license_viewer, "expense": expense_form, "preferences": preferences}

if __name__ == "__main__":
    APPS[sys.argv[1]]().show_all()
    Gtk.main()
