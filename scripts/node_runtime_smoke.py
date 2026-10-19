"""Check the packaged Node runtime with real npm installation and Vite startup."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

from moyai.coding_image import NODE_VERSION

from scripts.lambda_image import ROOT, package

BASE_IMAGES = (
    "public.ecr.aws/docker/library/python:3.13-slim",
    "docker.io/library/python:3.13-slim",
)

INSTALL_HOOK = """\
const fs = require("node:fs");
fs.writeFileSync("installed-runtime.json", JSON.stringify({
  version: process.version, executable: process.execPath, npmNode: process.env.npm_node_execpath
}));
"""

PREVIEW = """\
const fs = require("node:fs");
// Exercise the native dependency that was absent after installation on Node 18.
require("@tailwindcss/oxide");
(async () => {
  const { createServer } = await import("vite");
  const server = await createServer({
    configFile: false, root: process.cwd(),
    server: { host: "127.0.0.1", port: 8840, strictPort: true },
    plugins: [{ name: "runtime-proof", configureServer(vite) {
      vite.middlewares.use((req, res, next) => {
        if (req.url !== "/runtime") return next();
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({
          version: process.version, executable: process.execPath,
          npmNode: process.env.npm_node_execpath,
          installed: JSON.parse(fs.readFileSync("installed-runtime.json", "utf8"))
        }));
      });
    }}]
  });
  await server.listen();
})();
"""

CHECK = """\
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { spawn, spawnSync } = require("node:child_process");
const expected = "v" + process.argv[2];
assert.equal(process.version, expected);
let install = spawnSync("npm", ["ci", "--no-audit", "--no-fund"], {stdio: "inherit"});
assert.equal(install.status, 0, "locked dependency installation must succeed");
const preview = spawn("npm", ["run", "preview"], {stdio: "inherit", detached: true});
(async () => {
  try {
    let result;
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      if (preview.exitCode !== null) throw new Error("Preview exited before readiness");
      try {
        const response = await fetch("http://127.0.0.1:8840/runtime", {
          signal: AbortSignal.timeout(1000)
        });
        if (response.ok) { result = await response.json(); break; }
      } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(result, "preview must become ready");
    for (const stage of [result.installed, result]) {
      assert.equal(stage.version, expected, "installation and preview use the pin");
      assert.equal(stage.executable, process.execPath, "same Node binary at every stage");
      assert.equal(stage.npmNode, process.execPath, "npm uses that Node binary too");
    }
    const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));
    pkg.engines = {node: ">=99.0.0"};
    fs.writeFileSync("package.json", JSON.stringify(pkg));
    const lock = JSON.parse(fs.readFileSync("package-lock.json", "utf8"));
    lock.packages[""].engines = pkg.engines;
    fs.writeFileSync("package-lock.json", JSON.stringify(lock));
    const rejected = spawnSync("npm", ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
      {encoding: "utf8"});
    assert.notEqual(rejected.status, 0, "incompatible engines must fail installation");
    assert.match(rejected.stderr, /EBADENGINE/);
    console.log(JSON.stringify({node: expected, install: result.installed,
      preview: {version: result.version, executable: result.executable, npmNode: result.npmNode},
      incompatibleEngineRejected: true}));
  } finally {
    try { process.kill(-preview.pid, "SIGTERM"); } catch {}
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def pull_base_image(platform: str) -> str:
    """Try the official image's alternate registry when a pull is unavailable."""
    for image in BASE_IMAGES:
        try:
            result = subprocess.run(
                ["docker", "pull", "--platform", platform, image], check=False, timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(f"Timed out pulling {image}; trying the alternate registry", flush=True)
            continue
        if result.returncode == 0:
            return image
    raise RuntimeError("Could not pull the Python base image from ECR Public or Docker Hub")


def verify(platform: str) -> None:
    identity = uuid4().hex
    tag = f"moyai-node-smoke:{identity}"
    base_tag = f"moyai-node-base:{identity}"
    container = f"moyai-node-smoke-{identity}"
    with tempfile.TemporaryDirectory(prefix="moyai-node-smoke-") as directory:
        root = Path(directory)
        archive = root / "lambda.zip"
        package(archive)
        context = root / "image"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(context)
        probe = root / "probe"
        probe.mkdir()
        pkg = json.loads((ROOT / "package.json").read_text())
        pkg["scripts"] = {"postinstall": "node install.cjs", "preview": "node preview.cjs"}
        (probe / "package.json").write_text(json.dumps(pkg))
        shutil.copyfile(ROOT / "package-lock.json", probe / "package-lock.json")
        (probe / "install.cjs").write_text(INSTALL_HOOK)
        (probe / "preview.cjs").write_text(PREVIEW)
        (probe / "check.cjs").write_text(CHECK)
        try:
            base = pull_base_image(platform)
            subprocess.run(["docker", "tag", base, base_tag], check=True)
            subprocess.run([
                "docker", "build", "--platform", platform,
                "--build-arg", f"PYTHON_IMAGE={base_tag}",
                "--tag", tag, str(context),
            ], check=True)
            subprocess.run([
                "docker", "run", "--rm", "--name", container, "--platform", platform,
                "--mount", f"type=bind,source={probe},target=/probe,readonly",
                tag, "sh", "-c",
                'cp -R /probe /tmp/probe && cd /tmp/probe && node check.cjs "$1"',
                "sh", NODE_VERSION,
            ], check=True, timeout=300)
        finally:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)
            subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=False)
            subprocess.run(["docker", "image", "rm", base_tag], capture_output=True, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), required=True)
    verify(parser.parse_args().platform)
