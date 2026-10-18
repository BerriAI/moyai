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
Path("/var/lib/eval").mkdir(parents=True, exist_ok=True)
Path("/var/lib/eval/note_answer.json").write_text(json.dumps({
    "sha256": hashlib.sha256(text.strip().encode()).hexdigest(),
}))
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
subprocess.run(["xdotool", "type", "--delay", "5", "--window", window, text], check=True)
