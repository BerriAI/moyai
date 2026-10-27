"""Run co-located Kubernetes roles with the installer's state and activation guards."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PORTS = {"api": 8787, "coordinator": 8788, "broker": 8789, "worker": 8790}
DEPENDENCIES = {"api": ("coordinator",), "coordinator": (), "broker": ("coordinator",), "worker": ("broker",)}
MARKER = Path(os.environ.get("DATA_DIR", "/data")) / "ingress" / "ready"


def healthy(role: str) -> bool:
    from moyai.deployment_config import build_revision

    request = Request(
        f"http://127.0.0.1:{PORTS[role]}/health",
        headers={"Host": urlsplit(os.environ["MOYAI_PUBLIC_URL"]).netloc},
    )
    try:
        with urlopen(request, timeout=3) as response:
            return (
                response.status == 200
                and response.headers.get("X-Moyai-Core") == "pydantic-v1"
                and response.headers.get("X-Moyai-Role") == role
                and response.headers.get("X-Moyai-Build") == build_revision(os.environ)
                and bool(response.headers.get("X-Moyai-Owner"))
            )
    except (OSError, URLError):
        return False


def wait_for(roles: tuple[str, ...]) -> None:
    deadline = time.monotonic() + 900
    while not all(healthy(role) for role in roles):
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for Moyai role health.")
        time.sleep(2)


def close_ingress(*_args: object) -> None:
    MARKER.unlink(missing_ok=True)


def serve(role: str) -> None:
    from moyai.deployment_config import InstallationError, configuration, read_state
    from moyai.product_start import main

    environment = dict(os.environ)
    state = read_state(environment)
    if state is None or state["phase"] != "initialized":
        raise InstallationError("Wait for initialization to complete before starting roles.")
    values = configuration(environment, state, role=role)
    if values["MOYAI_PRODUCT_MODE"] != "existing" or state["revision"] != values["MOYAI_BUILD_SHA"]:
        raise InstallationError("Use the initialized split installation and its exact image revision.")
    os.environ.update(values)
    wait_for(DEPENDENCIES[role])
    main(["serve", "--host", "0.0.0.0", "--port", str(PORTS[role])])


def activate() -> None:
    def stop(*_args: object) -> None:
        close_ingress()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    close_ingress()
    while True:
        wait_for(tuple(PORTS))
        # Upstream activation persists and reconciles the original resume intent.
        # It verifies the public broker route before opening the ingress marker.
        result = subprocess.run([sys.executable, "-m", "moyai.deployment", "activate"], check=False)
        if result.returncode:
            close_ingress()
            raise RuntimeError("Moyai activation failed; inspect its persisted receipt and public broker route.")
        while all(healthy(role) for role in PORTS):
            time.sleep(5)
        close_ingress()


def main() -> None:
    command, *arguments = sys.argv[1:]
    if command in PORTS and not arguments:
        serve(command)
    elif command == "activate" and not arguments:
        activate()
    elif command == "probe" and len(arguments) == 1 and arguments[0] in PORTS:
        raise SystemExit(0 if healthy(arguments[0]) else 1)
    elif command == "close-ingress" and not arguments:
        close_ingress()
    else:
        raise SystemExit("Expected a product role, activate, probe ROLE, or close-ingress.")


if __name__ == "__main__":
    main()
