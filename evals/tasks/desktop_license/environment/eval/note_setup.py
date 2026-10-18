"""Types a random unsaved document into Mousepad and records only its hash."""
from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import time
from pathlib import Path

ticket = secrets.token_hex(3).upper()
owner = secrets.choice(["Priya", "Marcus", "Lena", "Tomas", "Aiko"])
text = (
    f"Release checklist {ticket}\n"
    f"Owner: {owner}\n"
    f"1. Freeze branch release-{secrets.randbelow(900) + 100}\n"
    f"2. Rotate staging key {secrets.token_hex(4)}\n"
    "3. Post changelog in #launches\n"
)
subprocess.Popen(["mousepad"], start_new_session=True,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    found = subprocess.run(["xdotool", "search", "--name", "Mousepad"], capture_output=True, text=True)
    if found.stdout.strip():
        break
    time.sleep(.1)
window = found.stdout.split()[-1]
time.sleep(1)
subprocess.run(["xdotool", "windowactivate", "--sync", window], check=False)
subprocess.run(["xdotool", "type", "--delay", "5", text], check=True)


def buffer_text() -> str:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    def walk(node, depth=0):
        if node is None or depth > 30:
            return None
        if node.get_role() == Atspi.Role.TEXT and node.get_text_iface() is not None:
            return node
        for index in range(node.get_child_count()):
            found = walk(node.get_child_at_index(index), depth + 1)
            if found is not None:
                return found
        return None

    desktop = Atspi.get_desktop(0)
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is not None and (app.get_name() or "").lower() == "mousepad":
            node = walk(app)
            if node is not None:
                return Atspi.Text.get_text(node, 0, Atspi.Text.get_character_count(node))
    return ""


# Hash what the buffer really holds: synthetic typing can drop or remap characters.
time.sleep(1)
typed = buffer_text().strip()
if len(typed) < 40:
    raise SystemExit(f"Mousepad buffer holds {len(typed)} characters after typing.")
Path("/var/lib/eval").mkdir(parents=True, exist_ok=True)
Path("/var/lib/eval/note_answer.json").write_text(json.dumps({
    "sha256": hashlib.sha256(typed.encode()).hexdigest(),
}))
