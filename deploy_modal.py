"""Deploy the shared workspace to Modal: uv run python deploy_modal.py.

Uses one always-on Modal Server. Keep max_containers=1 and strategy=recreate;
SQLite checkpoints require one writer, including during deployments.
"""
from pathlib import Path

import modal

ROOT = Path(__file__).parent
APP_NAME = "moyai-devin"
VOLUME_NAME = "hermes-workspace-state"

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git", "ca-certificates")
    .pip_install("uv==0.11.17")
    .add_local_file(ROOT / "pyproject.toml", "/opt/workspace/pyproject.toml", copy=True)
    .add_local_file(ROOT / "uv.lock", "/opt/workspace/uv.lock", copy=True)
    .run_commands("cd /opt/workspace && uv sync --frozen --no-dev")
    .add_local_dir(ROOT / "app", "/opt/workspace/app", copy=True)
    .add_local_dir(ROOT / "inference", "/opt/workspace/inference", copy=True)
    .add_local_dir(ROOT / "sandbox", "/opt/workspace/sandbox", copy=True)
    .env({"PYTHONUNBUFFERED": "1"})
)

# A named Secret is created by main(), before the app is deployed. No .env or
# local task data is part of the image or uploaded source tree.
config_secret = modal.Secret.from_name("hermes-workspace-config", required_keys=[
    "WORKSPACE_PASSWORD", "SESSION_SECRET", "ENCRYPTION_KEY",
    "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
])


@app.server(
    image=image, secrets=[config_secret], volumes={"/checkpoints": volume},
    port=8787, min_containers=1, max_containers=1,
    cpu=0.25, memory=1024, max_concurrency=100,
    startup_timeout=180, exit_grace_period=30,
    unauthenticated=True, routing_region="us-west",
)
class Web:
    @modal.enter()
    def start(self):
        import os
        import subprocess

        # Resolve Modal's actual URL before starting the application; do not
        # trust arbitrary request Host headers to decide the public origin.
        url = modal.Server.from_name(APP_NAME, "Web").get_url()
        if not url or not url.startswith("https://"):
            raise RuntimeError("Modal did not return the workspace HTTPS URL")
        environment = {
            **os.environ,
            "PUBLIC_URL": url,
            "DATA_DIR": "/tmp/hermes-workspace-data",
            "CHECKPOINT_DIR": "/checkpoints",
            "MODAL_VOLUME_NAME": VOLUME_NAME,
            "TRUST_MODAL_PROXY": "true",
        }
        self.process = subprocess.Popen([
            "/opt/workspace/.venv/bin/uvicorn", "app.main:app",
            "--host", "0.0.0.0", "--port", "8787", "--workers", "1",
            "--timeout-graceful-shutdown", "20",
        ], cwd="/opt/workspace", env=environment)

    @modal.exit()
    def stop(self):
        import subprocess

        if hasattr(self, "process") and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                self.process.kill()


def main():
    import asyncio
    import os
    import secrets

    from cryptography.fernet import Fernet
    from dotenv import set_key
    from app.config import Settings

    if Path.cwd() != ROOT:
        raise SystemExit("Run this command from the internal-devin project directory.")
    settings = Settings()
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise SystemExit("Configure Modal credentials in .env first.")
    private_env = ROOT / ".env"
    private_env.touch(mode=0o600, exist_ok=True)
    private_env.chmod(0o600)
    for field, value in {
        "WORKSPACE_PASSWORD": settings.workspace_password or secrets.token_urlsafe(24),
        "SESSION_SECRET": settings.session_secret or secrets.token_urlsafe(48),
        "ENCRYPTION_KEY": settings.encryption_key or Fernet.generate_key().decode(),
    }.items():
        set_key(str(private_env), field, value)
    settings = Settings()
    # Validate before publishing; this is also enforced by the app at boot.
    if len(settings.workspace_password) < 16:
        raise SystemExit("WORKSPACE_PASSWORD must be at least 16 characters.")
    values = {
        name.upper(): str(value).lower() if isinstance(value, bool) else str(value)
        for name, value in settings.model_dump().items()
        if value is not None and name not in {"public_url", "data_dir", "checkpoint_dir", "modal_volume_name"}
    }

    async def deploy():
        client = await modal.Client.from_credentials.aio(settings.modal_token_id, settings.modal_token_secret)
        # Pin CLI discovery to the same explicit account used for deployment.
        os.environ["MODAL_TOKEN_ID"] = settings.modal_token_id
        os.environ["MODAL_TOKEN_SECRET"] = settings.modal_token_secret
        await modal.Secret.objects.create.aio("hermes-workspace-config", values, allow_existing=True, client=client)
        await modal.Secret.from_name("hermes-workspace-config", client=client).update.aio(values)
        await app.deploy.aio(client=client, strategy="recreate")
        url = await modal.Server.from_name(APP_NAME, "Web", client=client).get_url.aio()
        print(f"Workspace URL: {url}")
        print("Workspace password is stored privately as WORKSPACE_PASSWORD in .env.")

    with modal.enable_output():
        asyncio.run(deploy())


if __name__ == "__main__":
    main()
