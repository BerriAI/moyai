"""Run inside the task image: python /tests/integration_smoke.py.

Uses only the local fixture, no model gateway or external provider calls. Build
this task's Dockerfile, copy tests/ to /tests, and add api.deepseek.com:127.0.0.1
when starting the disposable container. Do not run in a container with real trials.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from judge import integration_evidence, read_records


def post(url: str, model: str, key: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 4}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": "litellm", "X-LiteLLM-Version": "1.107.0.dev3"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> None:
    requests = Path("/logs/agent/stub_requests.jsonl")
    receipts = requests.with_name("litellm_routes.jsonl")
    if requests.exists() or receipts.exists():
        raise RuntimeError("Use a fresh disposable container; existing evidence must not be overwritten")
    credentials = json.loads(Path("/eval/credentials.json").read_text())
    stub = subprocess.Popen([
        "python3", "/eval/stub.py", "--log-path", str(requests),
        "--response-path", "/eval/stub_response.json", "--cert", "/usr/local/share/ca-certificates/api.deepseek.com.crt",
        "--key", "/etc/ssl/private/api.deepseek.com.key",
    ])
    try:
        for _ in range(90):
            try:
                with urllib.request.urlopen("https://api.deepseek.com/healthz", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Local fixture proxy failed to become ready; inspect litellm_proxy.log")
        direct = post("https://api.deepseek.com/v1/chat/completions", "deepseek-flash", credentials["DEEPSEEK_API_KEY"])
        assert direct["choices"][0]["message"]["content"] == "OK"
        time.sleep(0.5)
        assert not integration_evidence(read_records(requests), read_records(receipts), "1.107.0.dev3")["support_verified"]
        routed = post("http://127.0.0.1:4000/v1/chat/completions", "deepseek/deepseek-flash", credentials["LITELLM_FIXTURE_API_KEY"])
        assert routed["choices"][0]["message"]["content"] == "OK"
        for _ in range(100):
            evidence = integration_evidence(read_records(requests), read_records(receipts), "1.107.0.dev3")
            if evidence["support_verified"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Successful proxy call did not produce matching installed-SDK evidence")
        assert evidence["matched_routes"][0]["response_id"] == routed["id"]
        assert evidence["matched_routes"][0]["response_id"] != direct["id"]
        print(json.dumps({"direct_provider_passed_gate": False, "litellm_proxy_passed_gate": True,
                          "litellm_version": "1.107.0.dev3", "scope": evidence["scope"]}))
    finally:
        stub.terminate()
        stub.wait(timeout=10)


if __name__ == "__main__":
    main()
