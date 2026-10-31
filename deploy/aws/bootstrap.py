"""Install a fresh application on the Terraform host. Invoked over SSM, not at boot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

APP = Path("/srv/moyai/app")
CONTROL = Path("/opt/moyai-aws")
SLACK_KEYS = {
    "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_SIGNING_SECRET", "SLACK_TEAM_ID",
    "SLACK_SESSION_USERS", "GOOGLE_ALLOWED_DOMAINS",
}


def run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, input_text: str | None = None) -> str:
    # The private host log may contain service diagnostics. Never echo commands,
    # environment values, secret JSON, or captured provider error payloads.
    result = subprocess.run(command, check=False, env=env, cwd=cwd, input=input_text, text=True, capture_output=True)
    if result.returncode:
        write_private(CONTROL / "last-error.log", result.stdout + result.stderr)
        raise RuntimeError(f"{command[0]} failed; inspect /opt/moyai-aws/last-error.log privately on the host")
    return result.stdout


def aws(region: str, *args: str) -> dict:
    return json.loads(run(["aws", "--region", region, "--output", "json", *args]))


def write_private(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o600)


def database_url(host: str, username: str, password: str, database: str, ca: str) -> str:
    return f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}@{host}:5432/{database}?sslmode=verify-full&sslrootcert={ca}"


def validate_secret(secret: dict) -> None:
    if not isinstance(secret, dict):
        raise TypeError("Runtime secret must be a JSON object")
    allowed = SLACK_KEYS | {"WORKSPACE_PASSWORD", "database_password", "gateway_key"}
    if set(secret) - allowed:
        raise ValueError("Unexpected runtime secret fields")
    for name, value in secret.items():
        if not isinstance(value, str) or any(c in value for c in "\n\r\x00"):
            raise ValueError(f"Invalid single-line secret field: {name}")
    for name in ("WORKSPACE_PASSWORD", "database_password", "gateway_key"):
        if len(secret.get(name, "")) < 24:
            raise ValueError(f"Supply at least 24 characters for {name}")
    slack = [secret.get(name) for name in SLACK_KEYS]
    if any(slack) and not all(slack):
        raise ValueError("Slack setup requires all six Slack identity/OAuth settings")


def configure_database(config: dict, secret: dict, ca: Path) -> None:
    admin = json.loads(aws(config["region"], "secretsmanager", "get-secret-value", "--secret-id", config["database_secret_arn"])["SecretString"])
    environment = {**os.environ,
        "ADMIN_DSN": database_url(config["database_host"], admin["username"], admin["password"], "postgres", "/rds-ca.pem"),
        "APP_DB_PASSWORD": secret["database_password"],
    }
    program = """
import os, psycopg
from psycopg import sql
with psycopg.connect(os.environ['ADMIN_DSN'], autocommit=True) as c:
    if not c.execute("SELECT 1 FROM pg_roles WHERE rolname='moyai'").fetchone():
        c.execute('CREATE ROLE moyai LOGIN')
    c.execute(sql.SQL('ALTER ROLE moyai PASSWORD {}').format(sql.Literal(os.environ['APP_DB_PASSWORD'])))
    c.execute('GRANT moyai TO moyai_admin')
    c.execute('ALTER DATABASE moyai OWNER TO moyai')
"""
    run(["docker", "run", "--rm", "--network", "host", "--env", "ADMIN_DSN", "--env", "APP_DB_PASSWORD",
         "--volume", f"{ca}:/rds-ca.pem:ro", "--entrypoint", "/app/.venv/bin/python", config["moyai_image"], "-c", program], env=environment)


def pull_application(config: dict) -> None:
    repository = config.get("application_repository", "")
    private = bool(repository) and config["moyai_image"].startswith(repository + "@sha256:")
    registry = repository.split("/", 1)[0]
    if private:
        password = run(["aws", "--region", config["region"], "ecr", "get-login-password"])
        run(["docker", "login", "--username", "AWS", "--password-stdin", registry], input_text=password)
    try:
        run(["docker", "pull", config["moyai_image"]])
    finally:
        if private:
            run(["docker", "logout", registry])


def configure_gateway(config: dict, secret: dict) -> None:
    gateway = APP.parent / "gateway"
    gateway.mkdir(mode=0o700, exist_ok=True)
    configure_gateway_tls(gateway, config["moyai_image"])
    # JSON is a YAML subset. Values are encoded, not interpolated into YAML/shell.
    settings = {
        "model_list": [{"model_name": "bedrock/" + config["bedrock_model_id"], "litellm_params": {
            "model": "bedrock/" + config["bedrock_model_id"], "aws_region_name": config["region"],
        }}],
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
        "litellm_settings": {"drop_params": True, "telemetry": False},
    }
    write_private(gateway / "config.json", json.dumps(settings))
    (gateway / "config.json").chmod(0o644)  # Model routing only; the key is injected through env_file.
    write_private(gateway / "gateway.env", f"LITELLM_MASTER_KEY={secret['gateway_key']}\nAWS_REGION_NAME={config['region']}\n")
    compose = {
        "services": {"gateway": {
            "image": config["litellm_image"], "command": ["--config", "/config.json", "--port", "4000",
                "--ssl_keyfile_path", "/tls/key.pem", "--ssl_certfile_path", "/tls/cert.pem"],
            "env_file": [{"path": "gateway.env", "format": "raw"}],
            "volumes": ["./config.json:/config.json:ro", "./tls:/tls:ro"], "restart": "unless-stopped",
            "networks": ["model"],
            "healthcheck": {"test": ["CMD", "python", "-c", "import ssl, urllib.request; urllib.request.urlopen('https://localhost:4000/health/liveliness', context=ssl.create_default_context(cafile='/tls/cert.pem'), timeout=5)"],
                            "interval": "5s", "timeout": "10s", "retries": 36, "start_period": "20s"},
        }},
        "networks": {"model": {"name": "moyai-model"}},
    }
    write_private(gateway / "compose.json", json.dumps(compose))
    run(["docker", "compose", "-p", "moyai-gateway", "-f", "compose.json", "up", "-d", "--wait", "--wait-timeout", "240"], cwd=gateway)


def configure_gateway_tls(gateway: Path, image: str) -> None:
    """Keep the private gateway authenticated with a locally trusted TLS identity."""
    tls = gateway / "tls"
    tls.mkdir(mode=0o700, exist_ok=True)
    # Generate inside the pinned app image, which includes cryptography and certifi.
    # The key stays on the trusted host; only its public certificate goes to Moyai.
    program = """
from datetime import datetime, timedelta, timezone
from pathlib import Path
import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
tls = Path('/tls')
if not (tls / 'cert.pem').exists() and not (tls / 'key.pem').exists():
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Moyai private gateway')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('gateway'), x509.DNSName('localhost')]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(key, hashes.SHA256()))
    (tls / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (tls / 'key.pem').chmod(0o600)
    (tls / 'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
assert (tls / 'key.pem').is_file() and (tls / 'cert.pem').is_file(), 'Incomplete gateway TLS identity'
Path('/app-ca.pem').write_bytes(Path(certifi.where()).read_bytes() + b'\\n' + (tls / 'cert.pem').read_bytes())
"""
    bundle = APP / "gateway-ca-bundle.pem"
    bundle.touch(mode=0o600, exist_ok=True)
    run(["docker", "run", "--rm", "--network", "none", "--user", "0:0",
         "--volume", f"{tls}:/tls", "--volume", f"{bundle}:/app-ca.pem",
         "--entrypoint", "/app/.venv/bin/python", image, "-c", program])
    bundle.chmod(0o644)


def install(config: dict, archive: Path) -> None:
    if not (CONTROL / "host-ready").is_file():
        raise RuntimeError("Host bootstrap has not finished; inspect cloud-init-output.log")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != config["bundle_sha256"]:
        raise ValueError("Deployment bundle checksum mismatch")
    receipt_path = APP / "aws-installation.json"
    identity = {key: config[key] for key in (
        "url", "region", "database_host", "bucket", "secret_arn", "image_arn", "image_version",
        "moyai_image", "litellm_image", "bedrock_model_id", "computer_lifetime", "computer_idle_timeout", "bundle_sha256",
    )}
    if receipt_path.exists():
        if json.loads(receipt_path.read_text()) != identity:
            raise RuntimeError("Installation already exists. Use the documented maintenance upgrade; no configuration was changed.")
        run(["sh", "moyai.sh", "check"], cwd=APP)
        print("Existing installation retained; dependencies checked.")
        return
    pending = APP / "aws-installation-pending.json"
    if pending.exists() and json.loads(pending.read_text()) != identity:
        raise RuntimeError("An interrupted installation has different inputs; nothing was overwritten")
    if (APP / ".env").exists():
        if not pending.exists():
            raise RuntimeError("Unrecognized existing configuration; nothing was overwritten")
        complete_install(identity, receipt_path)
        return
    with tarfile.open(archive) as bundle:
        bundle.extractall(APP, filter="data")
    secret = json.loads(aws(config["region"], "secretsmanager", "get-secret-value", "--secret-id", config["secret_arn"])["SecretString"])
    validate_secret(secret)
    write_private(pending, json.dumps(identity, indent=2) + "\n")
    ca = APP / "rds-ca.pem"
    with urlopen("https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem", timeout=30) as response:
        ca.write_bytes(response.read())
    ca.chmod(0o644)  # The non-root application needs the public CA bundle.
    pull_application(config)
    configure_database(config, secret, ca)
    configure_gateway(config, secret)
    environment = {**os.environ, **{key: secret[key] for key in SLACK_KEYS if key in secret},
        "MOYAI_IMAGE": config["moyai_image"], "MOYAI_INSTALLATION_ID": "moyai", "MOYAI_INSTALL_TOPOLOGY": "split",
        "MOYAI_TEMPORAL_MODE": "bundled", "MOYAI_PUBLIC_URL": config["url"], "MOYAI_BIND_ADDRESS": "0.0.0.0",
        "MOYAI_DATABASE_URL": database_url(config["database_host"], "moyai", secret["database_password"], "moyai", "/run/rds-ca.pem"),
        "OBJECT_STORAGE_BUCKET": config["bucket"], "OBJECT_STORAGE_REGION": config["region"],
        "OBJECT_STORAGE_PREFIX": "moyai/product", "MOYAI_NATIVE_OBJECT_PREFIX": "moyai/native",
        "SANDBOX_PROVIDER": "lambda", "LAMBDA_REGION": config["region"], "LAMBDA_IMAGE": config["image_arn"],
        "LAMBDA_IMAGE_VERSION": config["image_version"], "LAMBDA_CHECKPOINT_BUCKET": config["bucket"],
        "LAMBDA_CHECKPOINT_PREFIX": "moyai/lambda", "MOYAI_COMPUTER_TIMEOUT": str(config["computer_lifetime"]),
        "MOYAI_COMPUTER_IDLE_TIMEOUT": str(config["computer_idle_timeout"]),
        "LITELLM_API_BASE": "https://gateway:4000/v1", "LITELLM_API_KEY": secret["gateway_key"], "AGENT_MODEL": "bedrock/" + config["bedrock_model_id"],
        "WORKSPACE_PASSWORD": secret["WORKSPACE_PASSWORD"], "SLACK_BOT_ENABLED": str(bool(secret.get("SLACK_CLIENT_ID"))).lower(),
    }
    run(["sh", "moyai.sh", "configure", "--from-env", "--topology", "split", "--temporal", "bundled"], env=environment, cwd=APP)
    complete_install(identity, receipt_path)


def complete_install(identity: dict, receipt_path: Path) -> None:
    """Resume after configuration publication without generating new keys/passwords."""
    services = ["setup", "api", "coordinator", "broker", "worker", "activate", "temporal-namespace"]
    overlay = {"services": {name: {"networks": ["default", "model"],
               "environment": {"SSL_CERT_FILE": "/run/gateway-ca-bundle.pem"},
               "volumes": ["./rds-ca.pem:/run/rds-ca.pem:ro", "./gateway-ca-bundle.pem:/run/gateway-ca-bundle.pem:ro"]} for name in services},
               "networks": {"model": {"external": True, "name": "moyai-model"}}}
    write_private(APP / "compose.aws.json", json.dumps(overlay))
    contents = (APP / ".env").read_text()
    lines = [line + ":compose.aws.json" if line.startswith("COMPOSE_FILE=") and ":compose.aws.json" not in line else line for line in contents.splitlines()]
    write_private(APP / ".env", "\n".join(lines) + "\n")
    # Do not expose the database/model/Slack secrets through SSM output.
    run(["sh", "moyai.sh", "up", "--timeout", "900"], cwd=APP)
    run(["sh", "moyai.sh", "check"], cwd=APP)
    write_private(receipt_path, json.dumps(identity, indent=2) + "\n")
    (APP / "aws-installation-pending.json").unlink(missing_ok=True)
    print("Moyai is ready at " + identity["url"] + "; complete Slack OAuth in Connections.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        install(json.loads(args.config.read_text()), args.archive)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, tarfile.TarError) as error:
        # Do not print exception payloads from secret-bearing subprocesses.
        print(f"Installation stopped ({type(error).__name__}): {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
