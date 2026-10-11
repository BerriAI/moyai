#!/usr/bin/env bash
# Reproducible native harness tests. No external inference or provider keys.
set -euo pipefail
cd "$(dirname "$0")/.."
runtime_root=$(mktemp -d)
trap 'rm -rf "$runtime_root"' EXIT
# Keep runtime-only dependencies out of the controller's locked environment.
UV_PROJECT_ENVIRONMENT="$runtime_root/venv" uv sync --frozen --python 3.13
runtime_python="$runtime_root/venv/bin/python"
uv pip install --python "$runtime_python" litellm==1.104.0 deepagents==0.7.22 langchain-litellm==0.11.0 mcp==2.2.0
revision=$("$runtime_python" -c 'from sandbox.harness_dependencies import LITELLM_REVISION; print(LITELLM_REVISION)')
pi_version=$("$runtime_python" -c 'from sandbox.harness_dependencies import PI_VERSION; print(PI_VERSION)')
node_version=$("$runtime_python" -c 'from sandbox.harness_dependencies import PI_NODE_VERSION; print(PI_NODE_VERSION)')
git init "$runtime_root/litellm"
git -C "$runtime_root/litellm" fetch --depth 1 https://github.com/BerriAI/litellm.git "$revision"
git -C "$runtime_root/litellm" checkout --detach FETCH_HEAD
npm install --prefix "$runtime_root" --no-audit --no-fund "node@$node_version" "@earendil-works/pi-coding-agent@$pi_version" opencode-ai@1.18.35
export PATH="$runtime_root/node_modules/.bin:$PATH"
export PYTHONPATH="$runtime_root/litellm"
export LITELLM_LOCAL_MODEL_COST_MAP=True MOYAI_REQUIRE_PI_RUNTIME=1
"$runtime_python" -m pytest -q tests/test_pi_runtime.py tests/test_mcp_bridge.py tests/test_harness_dependencies.py
"$runtime_python" -m pytest -q -s tests/test_harnesses.py -k 'real_litellm or catalog_covers or native_compaction_controls or real_deepagents'
